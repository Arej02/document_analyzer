"""Executor worker: execution_jobs status transitions — design_agent3.md §2, §10.

Agent 3 counterpart of agent2/worker/status.py, against Agent 3's own
`execution_jobs` table. Runs on the executor worker only, via the service-role
client (bypasses RLS). Every update filters on both `id` and `org_id`
(CLAUDE.md multi-tenancy rule), so a desync between "which job" and "which
org" is a no-op, never a write to another org's row.
"""

from supabase import Client

# execution_stage enum order (20260925100000_agent3_execution_tables.sql).
FIRST_STAGE = "prepare"

# unit_execution_status values that are a final verdict (specs FR7) — shared by
# the router's live counts and the worker's resume logic (design_agent3.md §3).
TERMINAL_UNIT_STATUSES = ("passed", "failed_verification", "error")


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


def mark_unit_running(client: Client, execution_job_id: str, org_id: str, cluster_id: str) -> None:
    """§4 step 6, run once at the start of a unit's processing. `unit_executions` has
    no `id` loaded anywhere in `ExecutionState` (`prepare.py` never selects it), so
    every unit update here is filtered by `(execution_job_id, cluster_id, org_id)` —
    the table's own unique constraint (§2) — instead of `id` + `org_id`."""
    (
        client.table("unit_executions")
        .update({"status": "running"})
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .eq("org_id", org_id)
        .execute()
    )


def update_unit_execution(client: Client, execution_job_id: str, org_id: str, cluster_id: str, **fields) -> None:
    """§4 step 6: the terminal (or in-progress) update — `status`, `attempts`,
    `symbol_map`, `test_summary`, `last_error`, `flags`, `storage_prefix`, whichever
    the caller passes. Same `(execution_job_id, cluster_id, org_id)` filter as
    `mark_unit_running`."""
    (
        client.table("unit_executions")
        .update(fields)
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .eq("org_id", org_id)
        .execute()
    )
