"""Planner worker: planning_jobs status transitions — design_agent2.md §1, §2.

Agent 2 counterpart of worker/status.py + job_completion.py's done transition,
against Agent 2's own `planning_jobs` table (never Agent 1's `ingestion_jobs`).
Runs on the planner worker only, via the service-role client (bypasses RLS).
Every update filters on both `id` and `org_id` (CLAUDE.md multi-tenancy rule),
so a desync between "which job" and "which org" is a no-op, never a write to
another org's row.
"""

from supabase import Client

# planning_stage enum order (20260923000000_agent2_planning_tables.sql).
FIRST_STAGE = "graph"


def mark_planning_running(client: Client, planning_job_id: str, org_id: str) -> None:
    """queued -> running, progress_stage -> graph (the first stage)."""
    (
        client.table("planning_jobs")
        .update({"status": "running", "progress_stage": FIRST_STAGE})
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_planning_stage(client: Client, planning_job_id: str, org_id: str, progress_stage: str) -> None:
    """Advances `progress_stage` within a running job: graph -> cluster -> report -> order."""
    (
        client.table("planning_jobs")
        .update({"progress_stage": progress_stage})
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_planning_failed(client: Client, planning_job_id: str, org_id: str, error: str) -> None:
    """Terminal failure: status -> failed, `error` set, `progress_stage` left as the
    stage the job died at (same convention as Agent 1's mark_job_failed)."""
    (
        client.table("planning_jobs")
        .update({"status": "failed", "error": error})
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_planning_done(client: Client, planning_job_id: str, org_id: str) -> None:
    """Terminal success: status -> done, progress_stage -> None (same as Agent 1's
    mark_job_done_if_ready)."""
    (
        client.table("planning_jobs")
        .update({"status": "done", "progress_stage": None})
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .execute()
    )
