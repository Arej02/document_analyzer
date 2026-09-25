"""Worker main loop — the piece flagged as missing throughout tasks.md Phases 1-4
("there's still no worker main loop that dequeues from `agent1_ingestion_jobs`
and strings [the individual worker steps] together in order").

Every step this module calls was already built and independently unit-tested;
this module's only job is ordering and wiring — dequeue a job, and for it in
order: mark it `running`, fetch its source, detect/boilerplate-filter, run
whichever `Extractor`s apply, persist entities/edges, diff the job's files
against the project's previous `done` job (diff.py, tasks.md Phase 5) and
record the changed/unchanged/removed counts, fan entity-level
summarization+grounding out concurrently (a content_hash-unchanged entity's
summary is reused via `summary_cache`, never re-sent to the LLM — the diff
step's counts are a separate, auditable record of the same fact, not a second
reuse mechanism), fold module -> subsystem -> repo summaries (each
individually grounded too), then mark the job `done` once every entity has a
resolved summary — deleting the job's github token and queue message no
matter how it ends.

Run via `uv run document-analyzer-worker` (see pyproject.toml), as a separate
long-lived process from `uvicorn` — this is the async worker CLAUDE.md's
"Async by default" rule assumes exists, never invoked from a FastAPI request.

`progress_stage` mapping (design.md §4's three `running` values): `extracting`
covers fetch/detect/boilerplate/parse/persist (no LLM calls); `summarizing`
covers the concurrent entity-level fan-out — grounding's regenerate-on-failure
retry (ground_retry.py) is itself an LLM generation call, so entity-level
grounding is inherently part of *generating* a fully-formed entity summary,
not a separable later phase; `grounding` covers the module -> subsystem -> repo
fold (fanned out per level, levels in order), the tail of the pipeline where a reviewer's
attention is on whether the rolled-up summaries actually hold up, not just
raw generation.

Known, deliberately out-of-scope-here limitations (flag, don't silently work
around, per CLAUDE.md's own convention in this codebase):
- `knowledge_summaries` has no `risk_factors` column (persist_summary.py) and
  no column naming which module/subsystem a level=module/subsystem row is for
  (cascade.py's documented schema gap) — a `summary_cache` hit reconstructs an
  entity's `risk_factors` as `[]` (only used for a rollup's own explanatory
  naming, never for the risk_level value itself, so this is lossy but not
  incorrect) since the original in-memory value isn't the cached row.
- One job is processed at a time, serially, by one worker process — Phase 6's
  concurrency/throughput hardening is explicitly future work (design.md §3:
  "revisit only if load testing shows it's an actual bottleneck").
"""

import asyncio
import logging
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before any env-reading import below (e.g. resource_bounds.py) — same as app.py;
# this is a separate process from uvicorn (CLAUDE.md "Async by default"), so it needs its own .env load.

from document_analyzer.github_token import delete_github_token, resolve_github_token
from document_analyzer.queue import delete_job_message, read_next_job
from document_analyzer.supabase_client import get_service_role_client
from document_analyzer.worker.boilerplate import filter_boilerplate, log_filtered_files
from document_analyzer.worker.diff import diff_against_previous_job, persist_diff_counts
from document_analyzer.worker.embedding_config_check import assert_embedding_config_in_sync
from document_analyzer.worker.extract import ExtractedEntity, ExtractionResult
from document_analyzer.worker.fetch import SourceFetchError, fetch_source
from document_analyzer.worker.ground import GroundingCheckError
from document_analyzer.worker.ground_retry import GroundingRetryResult, check_grounding_with_retry
from document_analyzer.worker.grounding_status import persist_grounding_status
from document_analyzer.worker.job_completion import mark_job_done_if_ready
from document_analyzer.worker.laravel_extractor import LaravelExtractor
from document_analyzer.worker.persist import persist_extraction_result
from document_analyzer.worker.persist_summary import persist_summary
from document_analyzer.worker.php_extractor import PhpExtractionError, PhpExtractor
from document_analyzer.worker.status import mark_job_failed, mark_job_running, mark_progress_stage
from document_analyzer.worker.summarize import (
    EntitySummary,
    SummarizationError,
    _entity_facts_payload,
    get_azure_openai_client,
    summarize_entity,
)
from document_analyzer.worker.summarize_fanout import SUMMARY_CONCURRENCY
from document_analyzer.worker.summarize_module import ModuleEntitySummary, _module_facts_payload, summarize_module
from document_analyzer.worker.summarize_subsystem import (
    ChildSummary,
    _children_payload,
    subsystem_for_file,
    summarize_repo,
    summarize_subsystem,
)
from document_analyzer.worker.summary_cache import copy_cached_summary_forward, lookup_cached_summary, write_through_cache
from document_analyzer.worker.wordpress_extractor import WordPressExtractionError, WordPressExtractor

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# Known extraction failure modes worth catching by name (so the job is marked
# `failed` with a specific, readable `error` rather than an opaque traceback);
# anything else still hits the catch-all in `process_job` below.
_PIPELINE_ERRORS = (SourceFetchError, PhpExtractionError, WordPressExtractionError, SummarizationError, GroundingCheckError)


# Same guard as Agent 2's planner worker (agent2/worker/run.py).
_TERMINAL_STATUSES = ("done", "failed")


_thread_local = threading.local()


def _client_for_thread():
    """One Supabase client per fan-out worker thread, not the single client `process_job`
    already holds. postgrest's client wraps one httpx.Client/HTTP2 connection, which isn't
    safe for concurrent reads from multiple threads at once — sharing it across
    SUMMARY_CONCURRENCY threads intermittently corrupted the connection's read state,
    surfacing on Windows as httpx.ReadError: [WinError 10035]. Caching per-thread (instead
    of a fresh client per entity) keeps this to ~SUMMARY_CONCURRENCY clients per job even
    when a repo has thousands of entities."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = get_service_role_client()
        _thread_local.client = client
    return client


@dataclass(frozen=True)
class _EntityOutcome:
    row: dict  # the persisted (or cache-hit) knowledge_summaries row
    file_path: str
    entity_type: str
    entity_name: str
    risk_factors: list[str]  # [] for a cache hit — see module docstring


def _fetch_job_row(client, job_id: str, org_id: str) -> dict:
    result = (
        client.table("ingestion_jobs")
        .select("id, project_id, source_type, source_ref, github_token_ref, status")
        .eq("id", job_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise SourceFetchError(f"ingestion_jobs row not found for job_id={job_id!r} org_id={org_id!r}")
    return result.data[0]


def _run_extractors(repo_path: Path, client, job_id: str, org_id: str) -> None:
    for extractor in (PhpExtractor(), WordPressExtractor(), LaravelExtractor()):
        if extractor.detect(repo_path):
            result: ExtractionResult = extractor.extract(repo_path)
            persist_extraction_result(client, job_id, org_id, result)


def _strip_boilerplate(repo_path: Path, client, job_id: str, org_id: str) -> None:
    """Deletes boilerplate files (vendor/, node_modules/, wp-includes/, .git/, ...) from
    the cloned/extracted copy on disk, not just from the audit log — extractors glob
    `*.php` over the whole repo_path, so e.g. vendor/'s own .php files would otherwise
    still get parsed as if they were the repo's own code."""
    all_files = [p for p in repo_path.rglob("*") if p.is_file()]
    _kept, filtered = filter_boilerplate(repo_path, all_files)
    log_filtered_files(client, job_id, org_id, filtered)
    for f in filtered:
        path = repo_path / f.file_path
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            # git marks its own pack/object files read-only; on Windows (unlike
            # POSIX, where unlink only needs write access to the *directory*) that
            # blocks a direct unlink. Clear the flag and retry once.
            path.chmod(stat.S_IWRITE)
            path.unlink(missing_ok=True)


def _process_one_entity(client, job_id: str, org_id: str, entity_row: dict, llm_client) -> _EntityOutcome:
    entity = ExtractedEntity(
        local_id=entity_row["id"],
        entity_type=entity_row["entity_type"],
        name=entity_row["name"],
        file_path=entity_row["file_path"],
        raw_facts=entity_row["raw_facts"],
    )

    cached = lookup_cached_summary(client, org_id, entity_row["content_hash"])
    if cached is not None:
        row = copy_cached_summary_forward(client, job_id, org_id, entity_row["id"], cached)
        return _EntityOutcome(
            row=row,
            file_path=entity.file_path,
            entity_type=entity.entity_type,
            entity_name=entity.name,
            risk_factors=[],
        )

    facts = _entity_facts_payload(entity)
    retry_result: GroundingRetryResult = check_grounding_with_retry(
        facts, generate=lambda: summarize_entity(entity, client=llm_client), client=llm_client
    )
    row = persist_summary(client, job_id, org_id, retry_result.summary, entity_id=entity_row["id"])
    persist_grounding_status(client, row["id"], org_id, retry_result)
    write_through_cache(client, org_id, entity_row["content_hash"], row["id"])

    return _EntityOutcome(
        row=row,
        file_path=entity.file_path,
        entity_type=entity.entity_type,
        entity_name=entity.name,
        risk_factors=retry_result.summary.risk_factors,
    )


def _summarize_entities(client, job_id: str, org_id: str, llm_client) -> list[_EntityOutcome]:
    entity_rows = (
        client.table("extracted_entities")
        .select("id, entity_type, name, file_path, raw_facts, content_hash")
        .eq("job_id", job_id)
        .eq("org_id", org_id)
        .execute()
        .data
    )
    if not entity_rows:
        return []

    with ThreadPoolExecutor(max_workers=SUMMARY_CONCURRENCY) as executor:
        return list(
            executor.map(
                lambda row: _process_one_entity(_client_for_thread(), job_id, org_id, row, llm_client), entity_rows
            )
        )


def _fold_module(client, job_id: str, org_id: str, file_path: str, entities: list[_EntityOutcome], llm_client) -> ChildSummary:
    entity_summaries = [
        ModuleEntitySummary(
            entity_name=e.entity_name,
            entity_type=e.entity_type,
            summary=EntitySummary(
                summary_text=e.row["summary_text"], risk_level=e.row["risk_level"], risk_factors=e.risk_factors
            ),
        )
        for e in entities
    ]
    facts = _module_facts_payload(file_path, entity_summaries)
    retry_result = check_grounding_with_retry(
        facts, generate=lambda: summarize_module(file_path, entity_summaries, client=llm_client), client=llm_client
    )
    row = persist_summary(client, job_id, org_id, retry_result.summary, entity_id=None)
    persist_grounding_status(client, row["id"], org_id, retry_result)
    return ChildSummary(name=file_path, summary_text=retry_result.summary.summary_text, risk_level=retry_result.summary.risk_level)


def _fold_children(client, job_id: str, org_id: str, name: str, level: str, child_level: str, children: list[ChildSummary], llm_client) -> ChildSummary:
    summarize_fn = summarize_subsystem if level == "subsystem" else summarize_repo
    args = (name, children) if level == "subsystem" else (children,)
    facts = _children_payload(name, children)
    retry_result = check_grounding_with_retry(
        facts, generate=lambda: summarize_fn(*args, client=llm_client), client=llm_client
    )
    row = persist_summary(client, job_id, org_id, retry_result.summary, entity_id=None)
    persist_grounding_status(client, row["id"], org_id, retry_result)
    return ChildSummary(name=name, summary_text=retry_result.summary.summary_text, risk_level=retry_result.summary.risk_level)


def _fold_ancestors(client, job_id: str, org_id: str, entity_outcomes: list[_EntityOutcome], llm_client) -> None:
    """Module folds are independent of each other (each sees only its own file's entity
    summaries), as are subsystem folds, so each level fans out through the same bounded
    pool as entity summarization; only the level-to-level dependency is sequential.
    Folding one module at a time made this the slowest part of a job (~2+ LLM calls
    per file, serially)."""
    by_module: dict[str, list[_EntityOutcome]] = {}
    for outcome in entity_outcomes:
        by_module.setdefault(outcome.file_path, []).append(outcome)

    with ThreadPoolExecutor(max_workers=SUMMARY_CONCURRENCY) as executor:
        module_children = list(
            executor.map(
                lambda item: _fold_module(_client_for_thread(), job_id, org_id, item[0], item[1], llm_client),
                by_module.items(),
            )
        )

        module_children_by_subsystem: dict[str, list[ChildSummary]] = {}
        for module_child in module_children:
            module_children_by_subsystem.setdefault(subsystem_for_file(module_child.name), []).append(module_child)

        subsystem_children = list(
            executor.map(
                lambda item: _fold_children(
                    _client_for_thread(), job_id, org_id, item[0], "subsystem", "module", item[1], llm_client
                ),
                module_children_by_subsystem.items(),
            )
        )

    _fold_children(client, job_id, org_id, "repo", "repo", "subsystem", subsystem_children, llm_client)


def process_job(job_id: str, org_id: str) -> None:
    """Runs one job's full pipeline to completion (or a handled failure). Never raises —
    any pipeline error is caught, written to `ingestion_jobs.error`, and the job is
    marked `failed`; the caller (run_forever) always deletes the queue message
    afterward either way, since a permanently-broken job (bad URL, unparseable
    archive) retrying forever would never succeed."""
    client = get_service_role_client()
    github_token_ref: str | None = None

    try:
        job_row = _fetch_job_row(client, job_id, org_id)
        if job_row.get("status") in _TERMINAL_STATUSES:
            # A redelivered message (the worker was stopped before deleting it) must not
            # re-run a finished job: that re-extracts every entity a second time and holds
            # up every job queued behind it.
            logger.info("job %s already %s; skipping", job_id, job_row["status"])
            return
        github_token_ref = job_row.get("github_token_ref")
        mark_job_running(client, job_id, org_id)

        job = {"source_type": job_row["source_type"], "source_ref": job_row["source_ref"]}
        if github_token_ref:
            job["access_token"] = asyncio.run(resolve_github_token(github_token_ref))

        with tempfile.TemporaryDirectory(prefix=f"agent1-job-{job_id}-") as tmp:
            dest_dir = Path(tmp)
            repo_path = fetch_source(client, job, dest_dir)
            _strip_boilerplate(repo_path, client, job_id, org_id)
            _run_extractors(repo_path, client, job_id, org_id)

        diff = diff_against_previous_job(client, org_id, job_row.get("project_id"), job_id)
        persist_diff_counts(client, job_id, org_id, diff)

        llm_client = get_azure_openai_client()

        mark_progress_stage(client, job_id, org_id, "summarizing")
        entity_outcomes = _summarize_entities(client, job_id, org_id, llm_client)

        if entity_outcomes:
            mark_progress_stage(client, job_id, org_id, "grounding")
            _fold_ancestors(client, job_id, org_id, entity_outcomes, llm_client)

        mark_job_done_if_ready(client, job_id, org_id)

    except _PIPELINE_ERRORS as exc:
        logger.warning("job %s failed: %s", job_id, exc)
        mark_job_failed(client, job_id, org_id, str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort net so one bad job can't kill the loop
        logger.exception("job %s failed with an unexpected error", job_id)
        mark_job_failed(client, job_id, org_id, f"unexpected error: {exc}")
    finally:
        if github_token_ref:
            asyncio.run(delete_github_token(github_token_ref))


def run_forever() -> None:
    """Polls `agent1_ingestion_jobs` forever, one job at a time. Ctrl+C to stop.

    Refuses to start at all if `embedding_config`'s active row has drifted from what
    embed.py actually loads (embedding_config_check.py, tasks.md Phase 6) — better to
    fail loudly here than silently embed a job's summaries under a pin nobody signed
    off on."""
    logging.basicConfig(level=logging.INFO)
    assert_embedding_config_in_sync(get_service_role_client())
    logger.info("worker started, polling %s every %ss when idle", "agent1_ingestion_jobs", POLL_INTERVAL_SECONDS)

    while True:
        message = asyncio.run(read_next_job())
        if message is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        logger.info("dequeued job %s (org %s)", message["job_id"], message["org_id"])
        process_job(message["job_id"], message["org_id"])
        asyncio.run(delete_job_message(message["msg_id"]))


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
