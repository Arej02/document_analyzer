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
