"""Planner worker main loop — design_agent2.md §1.

Agent 2's own long-lived worker process, separate from Agent 1's
(`document-analyzer-worker`) and from uvicorn (CLAUDE.md "Async by default",
"Each agent has its own queue and worker"). Polls `agent2_planning_jobs` and,
for each planning job, in order: mark it `running`, run the four stages
graph -> cluster -> report -> order (advancing `progress_stage` before each),
then mark it `done` — or `failed` with a readable `error` on any exception.
The queue message is deleted in a `finally` whatever happens, since a
permanently-broken job retrying forever would never succeed (same reasoning
as Agent 1's worker/run.py).

Run via `uv run document-analyzer-planner` (see pyproject.toml).

Each stage lives in its own module (graph.py, cluster.py, report.py, and
dependencies.py + test_contract.py + ordering.py for `order`). The in-memory
results one stage hands the next (the canonical graph, the clusters, the
report risk levels) travel in a per-job `PlanningState`.
"""

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # must run before any env-reading import below (design.md §3 bug #1) — separate process from uvicorn.

from supabase import Client

from document_analyzer.agent2.worker.cluster import ClusterResult, run_cluster_stage
from document_analyzer.agent2.worker.dependency_targets import run_dependency_targets_stage
from document_analyzer.agent2.worker.dependencies import run_dependencies_stage
from document_analyzer.agent2.worker.graph import CanonicalGraph, run_graph_stage
from document_analyzer.agent2.worker.ordering import delete_unfinished_plan, run_ordering_stage
from document_analyzer.agent2.worker.report import run_report_stage
from document_analyzer.agent2.worker.status import (
    mark_planning_done,
    mark_planning_failed,
    mark_planning_running,
    mark_planning_stage,
)
from document_analyzer.agent2.worker.test_contract import run_test_contract_stage
from document_analyzer.queue import PLANNING_QUEUE_NAME, delete_planning_job_message, read_next_planning_job
from document_analyzer.shared.llm import CallContext, LLMCostRecorder, MeteredLLMClient, new_run_id
from document_analyzer.supabase_client import get_service_role_client
from document_analyzer.worker.embed import embed_text
from document_analyzer.worker.embedding_config_check import assert_embedding_config_in_sync
from document_analyzer.worker.summarize import get_azure_openai_client

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# A redelivered message (visibility timeout elapsed, or a crash after the
# terminal write but before the delete) must not re-run a finished job.
_TERMINAL_STATUSES = ("done", "failed")


@dataclass
class PlanningState:
    """What one planning job's stages hand each other in memory (nothing here is
    re-read from the DB between stages)."""

    graph: CanonicalGraph | None = None
    clusters: ClusterResult | None = None
    # unit/phase cluster id -> cluster_reports.risk_level (§8 tie key)
    risk_by_cluster: dict[str, str] = field(default_factory=dict)
    plan_id: str | None = None
    # Cost accounting (design_agent2.md section 17): set per pickup by process_planning_job.
    # None when a stage is run on its own (tests), which leaves the client unmetered.
    cost_recorder: LLMCostRecorder | None = None
    cost_context: CallContext | None = None


StageFn = Callable[[Client, dict, PlanningState], None]


def stage_graph(client: Client, planning_job: dict, state: PlanningState) -> None:
    """design_agent2.md §3."""
    state.graph = run_graph_stage(client, planning_job)


def stage_cluster(client: Client, planning_job: dict, state: PlanningState) -> None:
    """design_agent2.md §4. An earlier attempt's plan references this job's clusters
    (no cascade, plans are immutable), so it goes before the clusters are rewritten."""
    delete_unfinished_plan(client, planning_job)
    state.clusters = run_cluster_stage(client, planning_job, state.graph)


def stage_report(client: Client, planning_job: dict, state: PlanningState) -> None:
    """design_agent2.md §6. The LLM client and embedder are looked up on this module
    at call time, so tests can swap them."""
    llm_client = get_azure_openai_client()
    if state.cost_recorder is not None and state.cost_context is not None:
        llm_client = MeteredLLMClient(
            llm_client, dataclasses.replace(state.cost_context, stage="report"), state.cost_recorder
        )
    try:
        outcomes = run_report_stage(
            client, planning_job, state.graph, state.clusters, llm_client=llm_client, embed=embed_text
        )
    finally:
        if state.cost_recorder is not None:
            state.cost_recorder.flush()
    state.risk_by_cluster = {o.cluster_id: o.report.risk_level for o in outcomes}


def stage_order(client: Client, planning_job: dict, state: PlanningState) -> None:
    """design_agent2.md §7, §10, §8: dependencies and cycles, test contracts, then
    ordering and plan version 1."""
    deps = run_dependencies_stage(client, planning_job, state.graph, state.clusters)
    contracts = run_test_contract_stage(client, planning_job, state.graph, deps.units)
    state.plan_id = run_ordering_stage(client, planning_job, state.clusters, deps, state.risk_by_cluster, contracts)
    _propose_dependency_targets(client, planning_job, state)


def _propose_dependency_targets(client: Client, planning_job: dict, state: PlanningState) -> None:
    """design_agent2.md §6.4. Best effort: the plan already exists, so a failure here is
    logged and the dependency inventory just has no targets."""
    try:
        llm_client = get_azure_openai_client()
        if state.cost_recorder is not None and state.cost_context is not None:
            llm_client = MeteredLLMClient(
                llm_client, dataclasses.replace(state.cost_context, stage="order"), state.cost_recorder
            )
        try:
            run_dependency_targets_stage(client, planning_job, state.plan_id, llm_client)
        finally:
            if state.cost_recorder is not None:
                state.cost_recorder.flush()
    except Exception:
        logger.warning("dependency targets failed for planning job %s", planning_job["id"], exc_info=True)


def _stages() -> list[tuple[str, StageFn]]:
    # Looked up at call time (not bound at import) so tests can monkeypatch the
    # module-level stage functions.
    return [("graph", stage_graph), ("cluster", stage_cluster), ("report", stage_report), ("order", stage_order)]


def _fetch_planning_job(client: Client, planning_job_id: str, org_id: str) -> dict | None:
    result = (
        client.table("planning_jobs")
        .select("id, org_id, project_id, source_job_id, status, params")
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def process_planning_job(planning_job_id: str, org_id: str) -> None:
    """Runs one planning job's stages to completion (or a handled failure). Never raises:
    any stage error is written to `planning_jobs.error` and the job is marked `failed`."""
    client = get_service_role_client()
    cost_recorder: LLMCostRecorder | None = None

    try:
        planning_job = _fetch_planning_job(client, planning_job_id, org_id)
        if planning_job is None:
            # Nothing to mark failed: no row matches this id + org_id.
            logger.warning("planning job %s (org %s) not found; dropping message", planning_job_id, org_id)
            return
        if planning_job["status"] in _TERMINAL_STATUSES:
            logger.info("planning job %s already %s; skipping", planning_job_id, planning_job["status"])
            return

        mark_planning_running(client, planning_job_id, org_id)
        # One recorder and one run_id per pickup; org_id comes from the job row.
        cost_recorder = LLMCostRecorder(client, planning_job["org_id"])
        state = PlanningState(
            cost_recorder=cost_recorder,
            cost_context=CallContext(
                agent="agent2", job_type="planning", job_id=planning_job_id, run_id=new_run_id()
            ),
        )
        for stage_name, stage_fn in _stages():
            mark_planning_stage(client, planning_job_id, org_id, stage_name)
            stage_fn(client, planning_job, state)
        mark_planning_done(client, planning_job_id, org_id)

    except Exception as exc:  # noqa: BLE001 — one bad job must never kill the loop
        logger.exception("planning job %s failed", planning_job_id)
        mark_planning_failed(client, planning_job_id, org_id, str(exc) or type(exc).__name__)
    finally:
        if cost_recorder is not None:
            cost_recorder.close()  # never raises


def run_once() -> bool:
    """Reads and handles at most one message. Returns whether a message was read.
    The message is deleted in `finally`, even if processing (or marking the job
    `failed`) itself raised."""
    message = asyncio.run(read_next_planning_job())
    if message is None:
        return False

    logger.info("dequeued planning job %s (org %s)", message["planning_job_id"], message["org_id"])
    try:
        process_planning_job(message["planning_job_id"], message["org_id"])
    finally:
        asyncio.run(delete_planning_job_message(message["msg_id"]))
    return True


def run_forever() -> None:
    """Polls `agent2_planning_jobs` forever, one job at a time. Ctrl+C to stop.

    Refuses to start if `embedding_config` has drifted from the pinned model
    (cluster report embeddings use the same pin as Agent 1, CLAUDE.md)."""
    logging.basicConfig(level=logging.INFO)
    assert_embedding_config_in_sync(get_service_role_client())
    logger.info("planner started, polling %s every %ss when idle", PLANNING_QUEUE_NAME, POLL_INTERVAL_SECONDS)

    while True:
        try:
            handled = run_once()
        except Exception:  # noqa: BLE001 — a queue/DB hiccup must not take the whole worker down
            logger.exception("planner loop iteration failed; continuing")
            handled = False
        if not handled:
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
