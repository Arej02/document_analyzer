"""Reporter worker main loop — design_report.md §4.

The report's own long-lived worker process, separate from the three agents' workers and
from uvicorn (CLAUDE.md "Async by default", "Each agent has its own queue and worker").
Polls `report_generation_jobs` and, for each job, in order: mark it `running`, run
collect -> estimate -> summarize -> render_html -> render_pdf -> publish (advancing
`progress_stage` and emitting `workflow_events` before each, with a cancellation check at
every stage boundary), then mark it `done`. Any exception before the report row exists
marks the job `failed` with a readable `error`; the queue message is deleted in a `finally`
whatever happens.

Read-only on Agents 1-3: the only writes are the report's own tables and workflow events.
Run via `uv run document-analyzer-reporter` (see pyproject.toml).

Artifacts (design_report.md §9.2): `render_html` runs the content gate, renders the pinned template,
runs the rendered gate and uploads the HTML; `render_pdf` renders it in the hardened container, gates
the extracted text and uploads the PDF. A failing PDF stage never fails the job. A queue message with
`stage = "render_pdf"` re-runs only the PDF for an already published report (`process_pdf_retry`).
"""

import asyncio
import dataclasses
import logging
import posixpath
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # must run before any env-reading import below (design.md §3 bug #1) — separate process from uvicorn.

from supabase import Client

from document_analyzer.queue import REPORT_QUEUE_NAME, delete_report_job_message, read_next_report_job
from document_analyzer.report import collect, exec_summary, leak_gate
from document_analyzer.report.build import SCHEMA_VERSION, build_report_json, plan_units
from document_analyzer.report.calibration import load_calibration
from document_analyzer.report.estimator import manual_effort, quote, simulate
from document_analyzer.report.pdf import PdfParams
from document_analyzer.report.render import TEMPLATE_VERSION, render_report_html  # pinned with the templates
from document_analyzer.report.worker import artifacts
from document_analyzer.report.worker.status import (
    STAGES,
    is_cancelled,
    mark_report_done,
    mark_report_failed,
    mark_report_running,
    mark_report_stage,
)
from document_analyzer.shared.llm import CallContext, LLMCostRecorder, MeteredLLMClient, new_run_id
from document_analyzer.shared.workflow_events import WorkflowEventEmitter
from document_analyzer.supabase_client import get_service_role_client
from document_analyzer.worker.summarize import get_azure_openai_client

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# A redelivered message must not re-run a finished job.
_TERMINAL_STATUSES = ("done", "failed", "cancelled")


DEFAULT_NEXT_STEPS = {"heading": "Next steps", "body": "", "contact": ""}

_STAGE_LABELS = {
    "collect": "Collect",
    "estimate": "Estimate",
    "summarize": "Executive summary",
    "render_html": "Render report",
    "render_pdf": "Render PDF",
    "publish": "Publish",
}


class ReportCancelled(Exception):
    """The job's status is `cancelled`; the pipeline stops without writing a report."""


@dataclass
class ReportState:
    """What one job's stages hand each other in memory."""

    emitter: WorkflowEventEmitter
    cost_recorder: LLMCostRecorder
    cost_context: CallContext
    config: dict
    config_hash: str
    project_name: str = ""
    snapshot_date: str = ""
    plan_row: dict | None = None
    estimate: dict | None = None  # the estimator's customer part (+ manual effort, quote)
    internal: dict | None = None  # operator-only part, goes to report_internal_estimates
    calibration_snapshot: dict | None = None
    executive_summary: dict | None = None
    report: dict | None = None
    pdf_status: str = "pending"
    html_path: str | None = None
    pdf_path: str | None = None
    report_id: str | None = None  # fixed in render_html so the artifact paths are deterministic
    html: str | None = None
    index: leak_gate.SourceIndex | None = None
    extra: dict = field(default_factory=dict)


def load_report_config() -> tuple[dict, str]:
    """`(estimator config, config hash)` from `report/config.py` (Phase 2 task, not yet built).
    Looked up on this module at call time so tests can inject a config."""
    try:
        from document_analyzer.report.config import load_config  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("report/config.py is not available; the estimator config cannot be loaded") from exc
    return load_config()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date(value) -> str:
    return str(value)[:10] if value else _now().date().isoformat()


def _project_name(source_ref: str) -> str:
    """The repo's own name (last path segment, no `.git`), never the whole URL."""
    tail = posixpath.basename(source_ref.rstrip("/").split("?")[0]).removesuffix(".git")
    return tail or "Project"


# --- stages ------------------------------------------------------------------------


def stage_collect(client: Client, job: dict, state: ReportState) -> None:
    """Checks the pin and eligibility again (the API already did, this is the run-time
    re-check) and gathers what the cover needs."""
    org_id = job["org_id"]
    source = (
        client.table("ingestion_jobs")
        .select("id, status, created_at, updated_at")
        .eq("id", job["source_job_id"])
        .eq("org_id", org_id)
        .limit(1)
        .execute()
        .data
    )
    if not source or source[0].get("status") != "done":
        raise collect.ReportInputError("the pinned Agent 1 job is missing or not done")
    project = (
        client.table("projects").select("source_ref").eq("id", job["project_id"]).eq("org_id", org_id).limit(1).execute().data
    )
    state.project_name = _project_name(project[0]["source_ref"]) if project else "Project"
    state.snapshot_date = _date(source[0].get("updated_at") or source[0].get("created_at"))
    if job["tier"] == "full":
        state.plan_row = collect.load_plan_row(client, org_id, job["plan_id"], job["source_job_id"])
        if state.plan_row["version"] != job["plan_version"]:
            raise collect.ReportInputError("the plan changed since the report was requested")
    state.emitter.activity(f"Inputs checked for {state.project_name}", refs=[{"kind": "id", "value": job["source_job_id"]}])


def stage_estimate(client: Client, job: dict, state: ReportState) -> None:
    """Full tier only: calibration (the one DB read here), the estimator, manual effort and,
    with `[pricing]`, a quote. The customer part goes into the report, the internal part into
    `report_internal_estimates` at publish."""
    if job["tier"] != "full":
        state.emitter.activity("X-ray has no estimate")
        return
    org_id, source_job_id, config = job["org_id"], job["source_job_id"], state.config
    plan, _ = collect.collect_plan(client, org_id, source_job_id, job["plan_id"])
    glance, _ = collect.collect_glance(client, org_id, source_job_id)
    risk, _ = collect.collect_risk(client, org_id, source_job_id)

    loaded = load_calibration(client, config)
    state.calibration_snapshot = loaded["snapshot"]
    sim = simulate(plan_units(plan), loaded["calibration"], config)
    risk_counts = {level: sum(row[level] for row in risk["heat_map"]) for level in ("low", "medium", "high")}
    manual = manual_effort(
        {
            "entities": glance.get("entities_by_type", {}),
            "risk_counts": risk_counts,
            "facade_cycles": len(plan["cycles_needing_facades"]),
            "oversized_units": len(plan["oversized_units"]),
        },
        config,
    )
    price = quote(sim, config)
    state.estimate = {**sim["customer"], "manual_effort": manual}
    if price is not None:
        state.estimate["quote"] = price
    cost = sim["internal"]["ai_cost_usd"]
    has_history = sim["internal"]["n"] > 0
    state.internal = {
        "ai_cost_p50": cost["p50"] if has_history else None,
        "ai_cost_p90": cost["p90"] if has_history else None,
        "n": sim["internal"]["n"],
        "calibration": {
            "snapshot": state.calibration_snapshot,
            "buckets": sim["internal"]["buckets"],
            "unpriced_excluded": sim["internal"]["unpriced_excluded"],
            "unpriced_share": sim["internal"]["unpriced_share"],
            "low_confidence": sim["internal"]["low_confidence"],
        },
        "price_basis": (
            {"method": price["method"], "currency": price["currency"], "pricing_version": config["pricing"].get("version")}
            if price is not None
            else None
        ),
    }
    state.emitter.activity(f"Estimated {sim['customer']['units']} unit(s) from {sim['customer']['n']} past unit(s)")


def _build(client: Client, job: dict, state: ReportState) -> dict:
    priced = state.estimate is not None and "quote" in state.estimate
    return build_report_json(
        client,
        job["org_id"],
        job_id=job["source_job_id"],
        tier=job["tier"],
        project_name=state.project_name,
        snapshot_date=state.snapshot_date,
        generated_at=_now().isoformat(),
        template_version=TEMPLATE_VERSION,
        estimator_version=str(state.config.get("version", "1")),
        estimator_config_hash=state.config_hash,
        next_steps=state.config.get("next_steps") or dict(DEFAULT_NEXT_STEPS),
        plan_id=job["plan_id"] if job["tier"] == "full" else None,
        estimate=state.estimate,
        pricing_version=(state.config.get("pricing") or {}).get("version") if priced else None,
        calibration_snapshot_id=(state.calibration_snapshot or {}).get("id"),
        executive_summary=state.executive_summary,
        config=state.config,
        calibration_snapshot=state.calibration_snapshot,
    )


def _repo_summary(client: Client, job: dict) -> str | None:
    rows = (
        client.table("knowledge_summaries")
        .select("summary_text")
        .eq("org_id", job["org_id"])
        .eq("job_id", job["source_job_id"])
        .eq("level", "repo")
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["summary_text"] if rows else None


def stage_summarize(client: Client, job: dict, state: ReportState) -> None:
    """The only LLM call. Never fails the job: a problem ends `unverified` or `withheld`.
    The report is built twice because the summary is written from the deterministic
    sections and the limitations depend on the summary's status."""
    try:
        draft = _build(client, job, state)
        facts = exec_summary.build_facts(draft, repo_summary=_repo_summary(client, job))
        llm = MeteredLLMClient(
            get_azure_openai_client(), dataclasses.replace(state.cost_context, stage="summarize"), state.cost_recorder
        )
        summary = exec_summary.generate_executive_summary(facts, client=llm)
    except Exception:  # noqa: BLE001
        logger.warning("executive summary failed for job %s", job["id"], exc_info=True)
        summary = {"text": "", "status": "withheld", "note": exec_summary.FAILED_NOTE}
    finally:
        state.cost_recorder.flush()
    if summary["status"] != "verified":
        state.emitter.activity(f"Executive summary is {summary['status']}", level="warn")
    state.executive_summary = summary
    state.report = _build(client, job, state)


def _existing_report_id(client: Client, job: dict) -> str | None:
    rows = (
        client.table("assessment_reports")
        .select("id")
        .eq("generation_job_id", job["id"])
        .eq("org_id", job["org_id"])
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["id"] if rows else None


def stage_render_html(client: Client, job: dict, state: ReportState) -> None:
    """Content gate, template render, rendered gate, upload (design_report.md §8.1, §9.2).

    A secret or marker outside a free-text field raises `LeakGateError` and fails the job; offending
    free text is replaced and listed in the limitations. A hit in the rendered text fails the job too,
    and nothing is uploaded: the artifact itself is the problem. The report id is the existing row's
    on a redelivery, else a new one, so the paths are deterministic."""
    org_id = job["org_id"]
    state.index = leak_gate.load_source_index(client, org_id, job["source_job_id"])
    state.report, hits, _ = leak_gate.apply_content_gate(state.report, state.index)
    for hit in hits:
        state.emitter.activity(f"Withheld {hit.path}: {', '.join(hit.reasons)}", level="warn")

    html = render_report_html(state.report)
    reasons = leak_gate.scan_rendered(leak_gate.html_text(html), state.index)
    if reasons:
        raise leak_gate.LeakGateError(f"the rendered report failed the content gate: {', '.join(reasons)}")
    state.report_id = _existing_report_id(client, job) or str(uuid.uuid4())
    state.html = html
    state.html_path = artifacts.html_path(org_id, state.report_id)
    artifacts.upload(client, state.html_path, html.encode("utf-8"), artifacts.HTML_CONTENT_TYPE)


def stage_render_pdf(client: Client, job: dict, state: ReportState) -> None:
    """Never fails the job: any problem leaves the report `done` with `pdf_status = failed`."""
    result = artifacts.render_and_store_pdf(
        client,
        org_id=job["org_id"],
        report_id=state.report_id,
        html=state.html,
        report_date=state.snapshot_date,
        params=PdfParams.from_job_params(job.get("params")),
        index=state.index,
    )
    state.pdf_status, state.pdf_path = result.status, result.path
    if result.status != "done":
        state.emitter.activity(f"PDF could not be generated: {result.reason}", level="warn")


def stage_publish(client: Client, job: dict, state: ReportState) -> None:
    """Inserts (or, on redelivery, updates) the `assessment_reports` row keyed by
    `generation_job_id`, then the internal estimate."""
    org_id, report = job["org_id"], state.report
    values = {
        "project_id": job["project_id"],
        "tier": job["tier"],
        "source_job_id": job["source_job_id"],
        "plan_id": job["plan_id"] if job["tier"] == "full" else None,
        "plan_version": state.plan_row["version"] if state.plan_row else None,
        "plan_status_at_generation": state.plan_row["status"] if state.plan_row else None,
        "report_json": report,
        "schema_version": SCHEMA_VERSION,
        "template_version": TEMPLATE_VERSION,
        "estimator_version": report["inputs"]["estimator_version"],
        "exec_summary_status": report["executive_summary"]["status"],
        "html_path": state.html_path,
        "pdf_path": state.pdf_path,
        "pdf_status": state.pdf_status,
        "created_by": job["created_by"],
    }
    table = client.table("assessment_reports")
    report_id = state.report_id
    if _existing_report_id(client, job):
        table.update(values).eq("id", report_id).eq("org_id", org_id).execute()
    else:
        table.insert({**values, "id": report_id, "org_id": org_id, "generation_job_id": job["id"]}).execute()
    if state.internal is not None:
        client.table("report_internal_estimates").upsert(
            {"org_id": org_id, "report_id": report_id, **state.internal}, on_conflict="org_id,report_id"
        ).execute()
    state.emitter.output(report_id, "report", f"{job['tier']} report", "created")


def _stages():
    # Looked up at call time (not bound at import) so tests can monkeypatch the stage functions.
    return [
        ("collect", stage_collect),
        ("estimate", stage_estimate),
        ("summarize", stage_summarize),
        ("render_html", stage_render_html),
        ("render_pdf", stage_render_pdf),
        ("publish", stage_publish),
    ]


def _fetch_job(client: Client, job_id: str, org_id: str) -> dict | None:
    rows = (
        client.table("report_generation_jobs")
        .select("id, org_id, project_id, tier, source_job_id, plan_id, plan_version, status, params, created_by")
        .eq("id", job_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def process_report_job(report_job_id: str, org_id: str) -> None:
    """Runs one report job's stages to completion (or a handled failure). Never raises: a stage
    error is written to `report_generation_jobs.error` and the job is marked `failed`."""
    client = get_service_role_client()
    recorder: LLMCostRecorder | None = None
    emitter: WorkflowEventEmitter | None = None

    try:
        job = _fetch_job(client, report_job_id, org_id)
        if job is None:
            logger.warning("report job %s (org %s) not found; dropping message", report_job_id, org_id)
            return
        if job["status"] in _TERMINAL_STATUSES:
            logger.info("report job %s already %s; skipping", report_job_id, job["status"])
            return

        mark_report_running(client, report_job_id, org_id)
        emitter = WorkflowEventEmitter(client, "report_generation", report_job_id, org_id)
        emitter.start([{"key": s, "label": _STAGE_LABELS[s]} for s in STAGES])
        recorder = LLMCostRecorder(client, org_id)
        config, config_hash = load_report_config()
        state = ReportState(
            emitter=emitter,
            cost_recorder=recorder,
            cost_context=CallContext(
                agent="report",
                job_type="report",
                job_id=report_job_id,
                run_id=new_run_id(),
                source_job_id=job["source_job_id"],
                requested_by=job["created_by"],
            ),
            config=config,
            config_hash=config_hash,
        )
        for stage_name, stage_fn in _stages():
            if is_cancelled(client, report_job_id, org_id):
                raise ReportCancelled(report_job_id)
            mark_report_stage(client, report_job_id, org_id, stage_name)
            emitter.stage(stage_name)
            stage_fn(client, job, state)
        if is_cancelled(client, report_job_id, org_id):
            raise ReportCancelled(report_job_id)
        mark_report_done(client, report_job_id, org_id)
        emitter.complete()

    except ReportCancelled:
        # The API already set status = cancelled; nothing to write but the event.
        logger.info("report job %s cancelled; stopping", report_job_id)
        if emitter is not None:
            emitter.cancel()
    except Exception as exc:  # noqa: BLE001 — one bad job must never kill the loop
        logger.exception("report job %s failed", report_job_id)
        message = str(exc) or type(exc).__name__
        mark_report_failed(client, report_job_id, org_id, message)
        if emitter is not None:
            emitter.fail(message)
    finally:
        if emitter is not None:
            emitter.flush()
        if recorder is not None:
            recorder.close()  # never raises


def process_pdf_retry(report_job_id: str, org_id: str) -> None:
    """The PDF-only retry (design_report.md §9.2): re-renders the *stored* HTML, so the PDF always
    matches the artifact the app shows, gates it and updates `pdf_path` / `pdf_status` on the existing
    report row. The job row and its events are not touched. Never raises."""
    client = get_service_role_client()
    report_id = None
    try:
        rows = (
            client.table("assessment_reports")
            .select("id, source_job_id, html_path, report_json")
            .eq("generation_job_id", report_job_id)
            .eq("org_id", org_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows or not rows[0].get("html_path"):
            logger.warning("pdf retry for job %s: no report with stored HTML; dropping message", report_job_id)
            return
        row = rows[0]
        report_id = row["id"]
        job = _fetch_job(client, report_job_id, org_id)
        html = artifacts.download(client, row["html_path"]).decode("utf-8")
        index = leak_gate.load_source_index(client, org_id, row["source_job_id"])
        result = artifacts.render_and_store_pdf(
            client,
            org_id=org_id,
            report_id=report_id,
            html=html,
            report_date=str(row["report_json"]["cover"]["snapshot_date"]),
            params=PdfParams.from_job_params((job or {}).get("params")),
            index=index,
        )
        values = {"pdf_status": result.status, "pdf_path": result.path}
    except Exception:  # noqa: BLE001 — one bad message must never kill the loop
        logger.exception("pdf retry for job %s failed", report_job_id)
        values = {"pdf_status": "failed", "pdf_path": None}
    if report_id is not None:
        client.table("assessment_reports").update(values).eq("id", report_id).eq("org_id", org_id).execute()


def run_once() -> bool:
    """Reads and handles at most one message. Returns whether a message was read. The message is
    deleted in `finally`, even if processing itself raised."""
    message = asyncio.run(read_next_report_job())
    if message is None:
        return False

    logger.info("dequeued report job %s (org %s)", message["report_job_id"], message["org_id"])
    try:
        if message.get("stage") == "render_pdf":
            process_pdf_retry(message["report_job_id"], message["org_id"])
        else:
            process_report_job(message["report_job_id"], message["org_id"])
    finally:
        asyncio.run(delete_report_job_message(message["msg_id"]))
    return True


def run_forever() -> None:
    """Polls `report_generation_jobs` forever, one job at a time. Ctrl+C to stop.

    The container engine and report image checks arrive with the PDF stage (Phase 5): a
    missing image only ever fails the PDF, never the report (design_report.md §4)."""
    logging.basicConfig(level=logging.INFO)
    logger.info("reporter started, polling %s every %ss when idle", REPORT_QUEUE_NAME, POLL_INTERVAL_SECONDS)

    while True:
        try:
            handled = run_once()
        except Exception:  # noqa: BLE001 — a queue/DB hiccup must not take the whole worker down
            logger.exception("reporter loop iteration failed; continuing")
            handled = False
        if not handled:
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
