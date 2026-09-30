"""Worker main loop — the piece flagged as missing throughout tasks.md Phases 1-4
("there's still no worker main loop that dequeues from `agent1_ingestion_jobs`
and strings [the individual worker steps] together in order").

Every step this module calls was already built and independently unit-tested;
this module's only job is ordering and wiring — dequeue a job, and for it in
order: mark it `running`, fetch its source, record which commit it cloned
(design_agent3.md §13), detect/boilerplate-filter, archive a post-filter
source snapshot (same Agent 3 prerequisite), run whichever `Extractor`s
apply (PHP/WordPress/Laravel, plus Graphify for every other code language),
build secret-redacted source evidence from the still-on-disk repo
(evidence.py), persist entities/edges, evidence chunks and the job's
extraction coverage report, diff the job's files
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

Live workflow run view (design.md §2, tasks.md): `process_job` builds one
`WorkflowEventEmitter` per job and threads it through every step below as an
explicit parameter (never a global — `shared/workflow_events.py`'s own
docstring), so it's still just a function argument under the fan-out thread
pools. `WORKFLOW_STAGES` here is Agent 1's own declared stage list; the
emitter never fails the job (CLAUDE.md "Grounding failures never block" —
same standing rule applies to event writes).
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
from document_analyzer.shared.workflow_events import WorkflowEventEmitter
from document_analyzer.supabase_client import get_service_role_client
from document_analyzer.worker.boilerplate import filter_boilerplate, log_filtered_files
from document_analyzer.worker.diff import diff_against_previous_job, persist_diff_counts
from document_analyzer.worker.embedding_config_check import assert_embedding_config_in_sync
from document_analyzer.worker.evidence import build_evidence, persist_evidence
from document_analyzer.worker.extract import ExtractedEntity, ExtractionResult
from document_analyzer.worker.fetch import SourceFetchError, fetch_source
from document_analyzer.worker.ground import GroundingCheckError
from document_analyzer.worker.graphify.extractor import GraphifyExtractionError, GraphifyExtractor
from document_analyzer.worker.ground_retry import GroundingRetryResult, check_grounding_with_retry
from document_analyzer.worker.grounding_status import grounding_status_for, persist_grounding_status
from document_analyzer.worker.cancellation import JobCancelled, forget_job, raise_if_cancelled
from document_analyzer.worker.discovery import discover_repo
from document_analyzer.worker.job_completion import mark_job_done_if_ready
from document_analyzer.worker.laravel_extractor import LaravelExtractor
from document_analyzer.worker.persist import persist_extraction_result
from document_analyzer.worker.persist_summary import persist_summary
from document_analyzer.worker.php_extractor import PhpExtractionError, PhpExtractor
from document_analyzer.worker.snapshot import SnapshotError, archive_source_snapshot
from document_analyzer.worker.status import (
    mark_job_failed,
    mark_job_running,
    mark_progress_stage,
    persist_extraction_coverage,
    persist_source_commit_sha,
    persist_source_snapshot_path,
)
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

# Live workflow run view (design.md §2) — declared once here, not hardcoded by the
# frontend. Finer-grained than progress_stage's three values above: extracting
# covers fetch/filter/snapshot/extract/evidence, summarizing is summarize,
# grounding is fold.
WORKFLOW_KIND = "agent1_ingestion"
WORKFLOW_STAGES = [
    {"key": "fetch", "label": "Fetch source"},
    {"key": "filter", "label": "Filter boilerplate"},
    {"key": "snapshot", "label": "Snapshot source"},
    {"key": "extract", "label": "Extract entities"},
    {"key": "evidence", "label": "Build evidence"},
    {"key": "summarize", "label": "Summarize entities"},
    {"key": "fold", "label": "Fold summaries"},
]

# Known extraction failure modes worth catching by name (so the job is marked
# `failed` with a specific, readable `error` rather than an opaque traceback);
# anything else still hits the catch-all in `process_job` below.
_PIPELINE_ERRORS = (
    SourceFetchError,
    PhpExtractionError,
    WordPressExtractionError,
    GraphifyExtractionError,
    SummarizationError,
    GroundingCheckError,
    SnapshotError,
)


# Same guard as Agent 2's planner worker (agent2/worker/run.py).
_TERMINAL_STATUSES = ("done", "failed", "cancelled")


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


class _Counter:
    """A plain int isn't safe to += from multiple threads at once; this is
    entity-summary fan-out's `summaries_done` progress counter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0

    def increment(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


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


def _extractors(job_id: str) -> tuple:
    """PHP first (nikic + the framework-wiring extractors), then Graphify for every other
    code language — GraphifyExtractor never takes a `.php` file (graphify/extractor.py)."""
    return (PhpExtractor(), WordPressExtractor(), LaravelExtractor(), GraphifyExtractor(job_id=job_id))


def _run_extractors(
    repo_path: Path, client, job_id: str, org_id: str, emitter: WorkflowEventEmitter
) -> dict[str, dict]:
    """Runs every applicable extractor, builds source evidence from the still-on-disk repo,
    persists entities/edges (each extractor's batch separately — local_ids are only unique
    within one ExtractionResult), then the evidence chunks linked to the new entity ids, then
    the job's coverage report. Returns extracted_entities.id -> evidence excerpt, which the
    entity-summary step passes to the summarizer and grounding check."""
    _enter_stage(emitter, client, job_id, org_id, "extract")
    applicable = [extractor for extractor in _extractors(job_id) if extractor.detect(repo_path)]
    results: list[ExtractionResult] = [extractor.extract(repo_path) for extractor in applicable]
    for extractor, result in zip(applicable, results):
        emitter.activity(
            f"{type(extractor).__name__}: {len(result.entities)} entities, {len(result.edges)} edges"
        )
    _warn_on_extraction_gaps(emitter, results)

    keyed = [((index, entity.local_id), entity) for index, result in enumerate(results) for entity in result.entities]
    evidence = build_evidence(repo_path, keyed)

    entity_ids: dict[tuple[int, str], str] = {}
    for index, result in enumerate(results):
        hashes = {
            entity.local_id: evidence.excerpt_hash((index, entity.local_id))
            for entity in result.entities
            if evidence.excerpt_hash((index, entity.local_id))
        }
        ids = persist_extraction_result(client, job_id, org_id, result, excerpt_hashes=hashes)
        entity_ids.update({(index, local_id): db_id for local_id, db_id in ids.items()})

    _enter_stage(emitter, client, job_id, org_id, "evidence")
    _warn_on_evidence_gaps(emitter, evidence.report)
    chunks_written = persist_evidence(client, job_id, org_id, evidence, entity_ids, {key: e.name for key, e in keyed})
    emitter.stats(evidence_chunks=chunks_written)

    coverage: dict = {"evidence": evidence.report}
    for result in results:
        coverage.update(result.coverage or {})
    persist_extraction_coverage(client, job_id, org_id, coverage)
    emitter.output("extraction_coverage", kind="coverage", label="Extraction coverage", status="done")

    return {entity_ids[key]: excerpt for key, excerpt in evidence.excerpts.items() if key in entity_ids}


def _strip_boilerplate(repo_path: Path, client, job_id: str, org_id: str, emitter: WorkflowEventEmitter) -> None:
    """Deletes boilerplate files (vendor/, node_modules/, wp-includes/, .git/, ...) from
    the cloned/extracted copy on disk, not just from the audit log — extractors glob
    `*.php` over the whole repo_path, so e.g. vendor/'s own .php files would otherwise
    still get parsed as if they were the repo's own code."""
    all_files = [p for p in repo_path.rglob("*") if p.is_file()]
    _kept, filtered = filter_boilerplate(repo_path, all_files)
    log_filtered_files(client, job_id, org_id, filtered)
    emitter.stats(boilerplate_filtered=len(filtered))
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


# Files named on one warning; the rest are counted in its message.
MAX_WARNING_FILES = 10


def _warn(emitter: WorkflowEventEmitter, message: str, paths: list[str]) -> None:
    """A user-visible warning: a `warn`-level activity event tagged with the open stage, its
    files as `file` refs (design.md §2). Warnings never fail or block the job."""
    emitter.activity(message, refs=[{"kind": "file", "value": p} for p in paths[:MAX_WARNING_FILES]], level="warn")


def _warn_on_extraction_gaps(emitter: WorkflowEventEmitter, results: list[ExtractionResult]) -> None:
    """Warns when extraction did not cover everything: Graphify unavailable (non-PHP code is
    not analysed) or files it could only partly extract. Read from each result's coverage report."""
    for result in results:
        graphify = (result.coverage or {}).get("graphify") or {}
        if graphify.get("status") == "unavailable":
            reason = graphify.get("reason") or "not available"
            _warn(emitter, f"Code in languages other than PHP was not analysed: Graphify is {reason}", [])
        count = graphify.get("not_extracted_count") or 0
        if graphify.get("status") == "ran" and count:
            paths = [f["path"] for f in graphify.get("not_extracted_sample") or [] if f.get("path")]
            noun = "file was" if count == 1 else "files were"
            _warn(emitter, f"{count} {noun} only partly extracted or not extracted (Graphify)", paths)


def _warn_on_evidence_gaps(emitter: WorkflowEventEmitter, report: dict) -> None:
    """Warns when some files could not be turned into source evidence, so summaries of their
    entities have less to be grounded against."""
    count = report.get("files_skipped_count") or 0
    if count:
        paths = [f["path"] for f in report.get("files_skipped") or [] if f.get("path")]
        noun = "file was" if count == 1 else "files were"
        _warn(emitter, f"{count} {noun} skipped when building source evidence", paths)


def _emit_discovered_stats(emitter: WorkflowEventEmitter, repo_path: Path) -> None:
    """Live "discovered so far" stats (languages, lines, frameworks, dependencies). Best effort:
    a failure here is logged and the job carries on, like every other progress signal."""
    try:
        emitter.stats(**discover_repo(repo_path))
    except Exception:  # noqa: BLE001
        logger.warning("could not compute discovered stats", exc_info=True)


def _enter_stage(emitter: WorkflowEventEmitter, client, job_id: str, org_id: str, key: str) -> None:
    """Stage boundary: stop here if the job was cancelled, otherwise announce the stage."""
    raise_if_cancelled(client, job_id, org_id)
    emitter.stage(key)


class _FoldProgress:
    """Counts the fold stage's summaries (modules, then subsystems, then the repo) for
    `progress.updated`. Thread-safe: module and subsystem folds run in a pool."""

    def __init__(self, emitter: WorkflowEventEmitter, total: int):
        self._emitter = emitter
        self._total = total
        self._counter = _Counter()

    def step(self, path: str) -> None:
        self._emitter.progress(self._counter.increment(), self._total, "summaries", path)


def _emit_entity_progress(
    emitter: WorkflowEventEmitter, entity: ExtractedEntity, status: str, cache_hit: bool, counter: _Counter, total: int
) -> None:
    verb = "cache hit" if cache_hit else "summarized"
    emitter.activity(
        f"{entity.name} ({entity.file_path}): {verb}",
        refs=[{"kind": "entity", "value": entity.name}, {"kind": "file", "value": entity.file_path}],
        level="warn" if status == "unverified" else "info",
    )
    done = counter.increment()
    emitter.stats(summaries_done=done, summaries_total=total)
    emitter.progress(done, total, "entities", entity.file_path)


def _process_one_entity(
    client,
    job_id: str,
    org_id: str,
    entity_row: dict,
    llm_client,
    excerpt: dict | None,
    emitter: WorkflowEventEmitter,
    counter: _Counter,
    total: int,
) -> _EntityOutcome:
    raise_if_cancelled(client, job_id, org_id)
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
        _emit_entity_progress(emitter, entity, row["grounding_status"], cache_hit=True, counter=counter, total=total)
        return _EntityOutcome(
            row=row,
            file_path=entity.file_path,
            entity_type=entity.entity_type,
            entity_name=entity.name,
            risk_factors=[],
        )

    facts = _entity_facts_payload(entity, excerpt)
    if excerpt:
        generate = lambda: summarize_entity(entity, client=llm_client, excerpt=excerpt)  # noqa: E731
    else:  # no stored evidence for this entity (no line info, unreadable file): facts only
        generate = lambda: summarize_entity(entity, client=llm_client)  # noqa: E731
    retry_result: GroundingRetryResult = check_grounding_with_retry(facts, generate=generate, client=llm_client)
    row = persist_summary(client, job_id, org_id, retry_result.summary, entity_id=entity_row["id"])
    persist_grounding_status(client, row["id"], org_id, retry_result)
    write_through_cache(client, org_id, entity_row["content_hash"], row["id"])
    _emit_entity_progress(
        emitter, entity, grounding_status_for(retry_result), cache_hit=False, counter=counter, total=total
    )

    return _EntityOutcome(
        row=row,
        file_path=entity.file_path,
        entity_type=entity.entity_type,
        entity_name=entity.name,
        risk_factors=retry_result.summary.risk_factors,
    )


def _summarize_entities(
    client, job_id: str, org_id: str, llm_client, excerpts: dict[str, dict] | None, emitter: WorkflowEventEmitter
) -> list[_EntityOutcome]:
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

    total = len(entity_rows)
    counter = _Counter()
    with ThreadPoolExecutor(max_workers=SUMMARY_CONCURRENCY) as executor:
        return list(
            executor.map(
                lambda row: _process_one_entity(
                    _client_for_thread(),
                    job_id,
                    org_id,
                    row,
                    llm_client,
                    (excerpts or {}).get(row["id"]),
                    emitter,
                    counter,
                    total,
                ),
                entity_rows,
            )
        )


def _fold_module(
    client,
    job_id: str,
    org_id: str,
    file_path: str,
    entities: list[_EntityOutcome],
    llm_client,
    emitter: WorkflowEventEmitter,
    progress: _FoldProgress | None = None,
) -> ChildSummary:
    raise_if_cancelled(client, job_id, org_id)
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
    emitter.output(f"module:{file_path}", kind="module_summary", label=file_path, status="done")
    if progress:
        progress.step(file_path)
    return ChildSummary(name=file_path, summary_text=retry_result.summary.summary_text, risk_level=retry_result.summary.risk_level)


def _fold_children(
    client,
    job_id: str,
    org_id: str,
    name: str,
    level: str,
    child_level: str,
    children: list[ChildSummary],
    llm_client,
    emitter: WorkflowEventEmitter,
    progress: _FoldProgress | None = None,
) -> ChildSummary:
    raise_if_cancelled(client, job_id, org_id)
    summarize_fn = summarize_subsystem if level == "subsystem" else summarize_repo
    args = (name, children) if level == "subsystem" else (children,)
    facts = _children_payload(name, children)
    retry_result = check_grounding_with_retry(
        facts, generate=lambda: summarize_fn(*args, client=llm_client), client=llm_client
    )
    row = persist_summary(client, job_id, org_id, retry_result.summary, entity_id=None)
    persist_grounding_status(client, row["id"], org_id, retry_result)
    emitter.output(f"{level}:{name}", kind=f"{level}_summary", label=name, status="done")
    if progress:
        progress.step(name)
    return ChildSummary(name=name, summary_text=retry_result.summary.summary_text, risk_level=retry_result.summary.risk_level)


def _fold_ancestors(
    client, job_id: str, org_id: str, entity_outcomes: list[_EntityOutcome], llm_client, emitter: WorkflowEventEmitter
) -> None:
    """Module folds are independent of each other (each sees only its own file's entity
    summaries), as are subsystem folds, so each level fans out through the same bounded
    pool as entity summarization; only the level-to-level dependency is sequential.
    Folding one module at a time made this the slowest part of a job (~2+ LLM calls
    per file, serially)."""
    by_module: dict[str, list[_EntityOutcome]] = {}
    for outcome in entity_outcomes:
        by_module.setdefault(outcome.file_path, []).append(outcome)

    # modules + subsystems + the repo summary, so the stage can report done / total.
    total = len(by_module) + len({subsystem_for_file(path) for path in by_module}) + 1
    progress = _FoldProgress(emitter, total)

    with ThreadPoolExecutor(max_workers=SUMMARY_CONCURRENCY) as executor:
        module_children = list(
            executor.map(
                lambda item: _fold_module(
                    _client_for_thread(), job_id, org_id, item[0], item[1], llm_client, emitter, progress
                ),
                by_module.items(),
            )
        )

        module_children_by_subsystem: dict[str, list[ChildSummary]] = {}
        for module_child in module_children:
            module_children_by_subsystem.setdefault(subsystem_for_file(module_child.name), []).append(module_child)

        subsystem_children = list(
            executor.map(
                lambda item: _fold_children(
                    _client_for_thread(), job_id, org_id, item[0], "subsystem", "module", item[1], llm_client, emitter, progress
                ),
                module_children_by_subsystem.items(),
            )
        )

    _fold_children(
        client, job_id, org_id, "repo", "repo", "subsystem", subsystem_children, llm_client, emitter, progress
    )


def _record_job_failure(client, job_id: str, org_id: str, error: str) -> None:
    """mark_job_failed, but never raises. A malformed queue message (non-uuid org_id,
    a job row that no longer exists) makes the status write fail too — letting that
    escape would crash the worker before run_forever deletes the message, so the same
    poison message would come back after every visibility timeout."""
    try:
        mark_job_failed(client, job_id, org_id, error)
    except Exception:  # noqa: BLE001
        logger.exception("could not record failure for job %s (org %s)", job_id, org_id)


def process_job(job_id: str, org_id: str) -> None:
    """Runs one job's full pipeline to completion (or a handled failure). Never raises —
    any pipeline error is caught, written to `ingestion_jobs.error`, and the job is
    marked `failed`; the caller (run_forever) always deletes the queue message
    afterward either way, since a permanently-broken job (bad URL, unparseable
    archive) retrying forever would never succeed."""
    client = get_service_role_client()
    github_token_ref: str | None = None
    emitter = WorkflowEventEmitter(client, WORKFLOW_KIND, job_id, org_id)

    try:
        job_row = _fetch_job_row(client, job_id, org_id)
        if job_row.get("status") in _TERMINAL_STATUSES:
            # A redelivered message (the worker was stopped before deleting it) must not
            # re-run a finished job: that re-extracts every entity a second time and holds
            # up every job queued behind it. No workflow.started was ever emitted for this
            # call, so there's nothing to complete/fail here either.
            logger.info("job %s already %s; skipping", job_id, job_row["status"])
            return
        github_token_ref = job_row.get("github_token_ref")
        mark_job_running(client, job_id, org_id)
        emitter.start(WORKFLOW_STAGES)

        job = {"source_type": job_row["source_type"], "source_ref": job_row["source_ref"]}
        if github_token_ref:
            job["access_token"] = asyncio.run(resolve_github_token(github_token_ref))

        with tempfile.TemporaryDirectory(prefix=f"agent1-job-{job_id}-") as tmp:
            dest_dir = Path(tmp)
            _enter_stage(emitter, client, job_id, org_id, "fetch")
            fetch_result = fetch_source(client, job, dest_dir)
            repo_path = fetch_result.repo_path
            persist_source_commit_sha(client, job_id, org_id, fetch_result.source_commit_sha)
            refs = [{"kind": "repo", "value": job_row["source_ref"]}]
            if fetch_result.source_commit_sha:
                refs.append({"kind": "commit", "value": fetch_result.source_commit_sha})
            emitter.activity("Fetched source", refs=refs)

            _enter_stage(emitter, client, job_id, org_id, "filter")
            _strip_boilerplate(repo_path, client, job_id, org_id, emitter)
            _emit_discovered_stats(emitter, repo_path)

            _enter_stage(emitter, client, job_id, org_id, "snapshot")
            snapshot_path = archive_source_snapshot(client, job_id, org_id, repo_path)
            persist_source_snapshot_path(client, job_id, org_id, snapshot_path)
            emitter.output("source_snapshot", kind="snapshot", label="Source snapshot", status="done")

            excerpts = _run_extractors(repo_path, client, job_id, org_id, emitter) or {}

        diff = diff_against_previous_job(client, org_id, job_row.get("project_id"), job_id)
        persist_diff_counts(client, job_id, org_id, diff)
        emitter.stats(
            changed_files=diff.changed_file_count,
            unchanged_files=diff.unchanged_file_count,
            removed_files=diff.removed_file_count,
        )

        llm_client = get_azure_openai_client()

        mark_progress_stage(client, job_id, org_id, "summarizing")
        _enter_stage(emitter, client, job_id, org_id, "summarize")
        entity_outcomes = _summarize_entities(client, job_id, org_id, llm_client, excerpts, emitter)

        if entity_outcomes:
            mark_progress_stage(client, job_id, org_id, "grounding")
            _enter_stage(emitter, client, job_id, org_id, "fold")
            _fold_ancestors(client, job_id, org_id, entity_outcomes, llm_client, emitter)

        # Unthrottled checks around the done transition: a cancel that lands here must not
        # be reported as a completed run.
        raise_if_cancelled(client, job_id, org_id, force=True)
        mark_job_done_if_ready(client, job_id, org_id)
        raise_if_cancelled(client, job_id, org_id, force=True)
        emitter.complete()

    except JobCancelled:
        # The API already set status = cancelled; nothing to write but the event.
        logger.info("job %s cancelled; stopping", job_id)
        emitter.cancel()
    except _PIPELINE_ERRORS as exc:
        logger.warning("job %s failed: %s", job_id, exc)
        _record_job_failure(client, job_id, org_id, str(exc))
        emitter.fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort net so one bad job can't kill the loop
        logger.exception("job %s failed with an unexpected error", job_id)
        _record_job_failure(client, job_id, org_id, f"unexpected error: {exc}")
        emitter.fail(f"unexpected error: {exc}")
    finally:
        forget_job(job_id)
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
