"""Executor worker: execution_jobs status transitions — design_agent3.md §2, §10.

Agent 3 counterpart of agent2/worker/status.py, against Agent 3's own
`execution_jobs` table. Runs on the executor worker only, via the service-role
client (bypasses RLS). Every update filters on both `id` and `org_id`
(CLAUDE.md multi-tenancy rule), so a desync between "which job" and "which
org" is a no-op, never a write to another org's row.
"""

from datetime import datetime, timezone

from supabase import Client

# execution_stage enum order (20260925100000_agent3_execution_tables.sql).
FIRST_STAGE = "prepare"

# unit_execution_status values that are a final verdict (specs FR7) — shared by
# the router's live counts and the worker's resume logic (design_agent3.md §3).
TERMINAL_UNIT_STATUSES = ("passed", "failed_verification", "error")

# PostgREST caps a response at 1000 rows; unit_executions rows are paged past it
# (same pattern as router.py / prepare.py), and job ids are chunked for `in_`.
_PAGE_SIZE = 1000
_IN_CHUNK = 200


def mark_execution_running(client: Client, execution_job_id: str, org_id: str) -> None:
    """queued -> running, progress_stage -> prepare (the first stage)."""
    (
        client.table("execution_jobs")
        .update({"status": "running", "progress_stage": FIRST_STAGE})
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_execution_stage(client: Client, execution_job_id: str, org_id: str, progress_stage: str) -> None:
    """Advances `progress_stage` within a running job: prepare -> units -> assemble -> final_test."""
    (
        client.table("execution_jobs")
        .update({"progress_stage": progress_stage})
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_execution_failed(client: Client, execution_job_id: str, org_id: str, error: str) -> None:
    """Terminal failure: status -> failed, `error` set, `progress_stage` left as the
    stage the job died at (same convention as Agents 1 and 2). Only for job-level
    (infrastructure) failures; a unit's failure is a unit verdict, never this."""
    (
        client.table("execution_jobs")
        .update({"status": "failed", "error": error})
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_execution_done(client: Client, execution_job_id: str, org_id: str) -> None:
    """Terminal success: status -> done, progress_stage -> None. `done` means every
    unit reached a terminal verdict, not that every unit passed (specs FR1)."""
    (
        client.table("execution_jobs")
        .update({"status": "done", "progress_stage": None})
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_unit_running(client: Client, execution_job_id: str, org_id: str, cluster_id: str) -> None:
    """§4 step 6, run once at the start of a unit's processing. `unit_executions` has
    no `id` loaded anywhere in `ExecutionState` (`prepare.py` never selects it), so
    every unit update here is filtered by `(execution_job_id, cluster_id, org_id)` —
    the table's own unique constraint (§2) — instead of `id` + `org_id`.

    `started_at` is set only the first time, so it survives outer rounds and redeliveries."""
    existing = (
        client.table("unit_executions")
        .select("started_at")
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .eq("org_id", org_id)
        .execute()
    ).data
    fields: dict = {"status": "running"}
    if not (existing and existing[0].get("started_at")):
        fields["started_at"] = _now()
    (
        client.table("unit_executions")
        .update(fields)
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .eq("org_id", org_id)
        .execute()
    )


def update_unit_execution(client: Client, execution_job_id: str, org_id: str, cluster_id: str, **fields) -> None:
    """§4 step 6: the terminal (or in-progress) update — `status`, `attempts`,
    `symbol_map`, `test_summary`, `last_error`, `flags`, `storage_prefix`, whichever
    the caller passes. Same `(execution_job_id, cluster_id, org_id)` filter as
    `mark_unit_running`. A terminal `status` also stamps `finished_at`."""
    if fields.get("status") in TERMINAL_UNIT_STATUSES:
        fields = {**fields, "finished_at": _now()}
    (
        client.table("unit_executions")
        .update(fields)
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .eq("org_id", org_id)
        .execute()
    )


def set_execution_output(
    client: Client, execution_job_id: str, org_id: str, *, output_zip_path: str, counts: dict, final_test_summary: dict
) -> None:
    """Stage `final_test` (design_agent3.md §5), the last write before the stage loop's
    own unconditional `mark_execution_done(...)` sets `status`/`progress_stage`. Same
    `(id, org_id)` filter as every other execution_jobs write here."""
    (
        client.table("execution_jobs")
        .update({"output_zip_path": output_zip_path, "counts": counts, "final_test_summary": final_test_summary})
        .eq("id", execution_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def _empty_counts() -> dict:
    return {"total": 0, "done": 0, "passed": 0, "failed_verification": 0, "error": 0, "built_on_failed_dependency": 0}


def counts_by_job(client: Client, org_id: str, execution_job_ids: list[str]) -> dict[str, dict]:
    """Live unit counts per execution job (design_agent3.md §2, §9), read in batches
    rather than one query per job. Moved here from `agent3/router.py` (2026-09-28):
    both the router's live status poll and stage `final_test`'s persisted `counts`
    need the same computation, and this module (not the router) is the shared home,
    since the worker must never import from the router (CLAUDE.md agent-boundary rule
    applies within an agent's own router/worker split too — wrong direction either way)."""
    counts = {job_id: _empty_counts() for job_id in execution_job_ids}
    for i in range(0, len(execution_job_ids), _IN_CHUNK):
        chunk = execution_job_ids[i : i + _IN_CHUNK]
        start = 0
        while True:
            page = (
                client.table("unit_executions")
                .select("id, execution_job_id, status, flags")
                .eq("org_id", org_id)
                .in_("execution_job_id", chunk)
                .order("id")
                .range(start, start + _PAGE_SIZE - 1)
                .execute()
            ).data
            for unit in page:
                job_counts = counts[unit["execution_job_id"]]
                job_counts["total"] += 1
                if unit["status"] in TERMINAL_UNIT_STATUSES:
                    job_counts["done"] += 1
                    job_counts[unit["status"]] += 1
                if "built_on_failed_dependency" in (unit.get("flags") or []):
                    job_counts["built_on_failed_dependency"] += 1
            if len(page) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
    return counts
