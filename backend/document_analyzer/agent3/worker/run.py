"""Executor worker main loop — design_agent3.md §1, §10.

Agent 3's own long-lived worker process, separate from Agent 1's and Agent 2's
workers and from uvicorn (CLAUDE.md "Async by default", "Each agent has its
own queue and worker"). Polls `agent3_execution_jobs` and, for each execution
job, in order: mark it `running`, run the four stages
prepare -> units -> assemble -> final_test (advancing `progress_stage` before
each), then mark it `done` — or `failed` with a readable `error` on any
exception. The queue message is deleted in a `finally` whatever happens, same
reasoning as Agents 1 and 2.

Run via `uv run document-analyzer-executor` (see pyproject.toml).

Each stage will live in its own module (context.py, generate.py, tests_gen.py,
sandbox.py, assemble.py). The in-memory results one stage hands the next travel
in a per-job `ExecutionState`. The stages are stubs until Phases 2-4
(tasks_agent3.md); a stub raises, so a job can never be marked `done` without
having generated anything.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # must run before any env-reading import below (design.md §3 bug #1) — separate process from uvicorn.

from supabase import Client

from document_analyzer.agent3.worker.sandbox import SandboxParams, check_sandbox_available, require_gvisor_from_env
from document_analyzer.agent3.worker.status import (
    mark_execution_done,
    mark_execution_failed,
    mark_execution_running,
    mark_execution_stage,
)
from document_analyzer.queue import EXECUTION_QUEUE_NAME, delete_execution_job_message, read_next_execution_job
from document_analyzer.supabase_client import get_service_role_client

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# A redelivered message (visibility timeout elapsed, or a crash after the
# terminal write but before the delete) must not re-run a finished job.
_TERMINAL_STATUSES = ("done", "failed")


@dataclass
class ExecutionState:
    """What one execution job's stages hand each other in memory (the Agent 2
    `PlanningState` pattern). Filled in by the stages as they're built."""

    plan: dict | None = None
    # migration_plan_items rows in (phase_order, unit_order) order (§3).
    items: list[dict] = field(default_factory=list)
    # cluster_id -> symbol map of every unit finished so far, for its dependents (§4).
    symbol_maps: dict[str, dict] = field(default_factory=dict)


StageFn = Callable[[Client, dict, ExecutionState], None]


def _not_implemented(stage: str) -> None:
    raise NotImplementedError(f"stage '{stage}' is not implemented yet (tasks_agent3.md)")


def stage_prepare(client: Client, execution_job: dict, state: ExecutionState) -> None:
    """design_agent3.md §3. Stub until tasks_agent3.md Phase 2."""
    _not_implemented("prepare")


def stage_units(client: Client, execution_job: dict, state: ExecutionState) -> None:
    """design_agent3.md §4. Stub until tasks_agent3.md Phase 3."""
    _not_implemented("units")


def stage_assemble(client: Client, execution_job: dict, state: ExecutionState) -> None:
    """design_agent3.md §5. Stub until tasks_agent3.md Phase 4."""
    _not_implemented("assemble")


def stage_final_test(client: Client, execution_job: dict, state: ExecutionState) -> None:
    """design_agent3.md §5. Stub until tasks_agent3.md Phase 4."""
    _not_implemented("final_test")


def _stages() -> list[tuple[str, StageFn]]:
    # Looked up at call time (not bound at import) so tests can monkeypatch the
    # module-level stage functions.
    return [
        ("prepare", stage_prepare),
        ("units", stage_units),
        ("assemble", stage_assemble),
        ("final_test", stage_final_test),
    ]


def _fetch_execution_job(client: Client, execution_job_id: str, org_id: str) -> dict | None:
    result = (
        client.table("execution_jobs")
        .select("id, org_id, project_id, plan_id, source_job_id, status, params")
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def process_execution_job(execution_job_id: str, org_id: str) -> None:
    """Runs one execution job's stages to completion (or a handled failure). Never raises:
    any stage error is written to `execution_jobs.error` and the job is marked `failed`."""
    client = get_service_role_client()

    try:
        execution_job = _fetch_execution_job(client, execution_job_id, org_id)
        if execution_job is None:
            # Nothing to mark failed: no row matches this id + org_id.
            logger.warning("execution job %s (org %s) not found; dropping message", execution_job_id, org_id)
            return
        if execution_job["status"] in _TERMINAL_STATUSES:
            logger.info("execution job %s already %s; skipping", execution_job_id, execution_job["status"])
            return

        mark_execution_running(client, execution_job_id, org_id)
        state = ExecutionState()
        for stage_name, stage_fn in _stages():
            mark_execution_stage(client, execution_job_id, org_id, stage_name)
            stage_fn(client, execution_job, state)
        mark_execution_done(client, execution_job_id, org_id)

    except Exception as exc:  # noqa: BLE001 — one bad job must never kill the loop
        logger.exception("execution job %s failed", execution_job_id)
        mark_execution_failed(client, execution_job_id, org_id, str(exc) or type(exc).__name__)


def run_once() -> bool:
    """Reads and handles at most one message. Returns whether a message was read.
    The message is deleted in `finally`, even if processing (or marking the job
    `failed`) itself raised."""
    message = asyncio.run(read_next_execution_job())
    if message is None:
        return False

    logger.info("dequeued execution job %s (org %s)", message["execution_job_id"], message["org_id"])
    try:
        process_execution_job(message["execution_job_id"], message["org_id"])
    finally:
        asyncio.run(delete_execution_job_message(message["msg_id"]))
    return True


def check_sandbox_at_startup() -> None:
    """Refuses to start (SandboxUnavailableError) unless the configured engine is
    reachable (and, for Colima, really is that profile — shared/container_engine.py), the runner image exists,
    and — when AGENT3_REQUIRE_GVISOR is set — gVisor is registered and selected
    (design_agent3.md §7, §10). Without gVisor it warns and carries on, every other
    sandbox flag still applies (developer machines: neither Colima's VM nor Docker Desktop has `runsc` by default)."""
    params = SandboxParams.from_env()
    check_sandbox_available(params, require_gvisor=require_gvisor_from_env())
    if params.runtime != "runsc":
        logger.warning(
            "sandbox is running without gVisor (AGENT3_SANDBOX_RUNTIME is not 'runsc'); "
            "fine for development, required in production (design_agent3.md §7)"
        )


def run_forever() -> None:
    """Polls `agent3_execution_jobs` forever, one job at a time. Ctrl+C to stop.

    Refuses to start if the sandbox isn't available. There's no embedding config
    check: Agent 3 doesn't embed (design_agent3.md §10)."""
    logging.basicConfig(level=logging.INFO)
    check_sandbox_at_startup()
    logger.info("executor started, polling %s every %ss when idle", EXECUTION_QUEUE_NAME, POLL_INTERVAL_SECONDS)

    while True:
        try:
            handled = run_once()
        except Exception:  # noqa: BLE001 — a queue/DB hiccup must not take the whole worker down
            logger.exception("executor loop iteration failed; continuing")
            handled = False
        if not handled:
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
