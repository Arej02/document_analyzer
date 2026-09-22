"""Worker step: job status transitions — tasks.md Phase 1
"Update ingestion_jobs.status -> running, progress_stage -> extracting".

Runs on the async worker only, via the service-role client (bypasses RLS) —
see supabase_client.get_service_role_client. This is the first write the
worker makes after dequeuing a job. Filtered by both id and org_id, not just
id, as a defense-in-depth match for the org-scoping discipline CLAUDE.md calls
out as security-critical for every worker write.

`mark_progress_stage`/`mark_job_failed` are the worker main loop's (worker/run.py)
other two status writes — advancing `progress_stage` within a running job, and
the terminal failure transition (the mirror image of job_completion.py's
`mark_job_done_if_ready` on the success path).
"""

from supabase import Client


def mark_job_running(client: Client, job_id: str, org_id: str) -> None:
    """Transitions a freshly-dequeued job: queued -> running, progress_stage -> extracting."""
    (
        client.table("ingestion_jobs")
        .update({"status": "running", "progress_stage": "extracting"})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_progress_stage(client: Client, job_id: str, org_id: str, progress_stage: str) -> None:
    """Advances `progress_stage` within an already-`running` job (design.md §4:
    `extracting` -> `summarizing` -> `grounding`, polled by the frontend's job status page)."""
    (
        client.table("ingestion_jobs")
        .update({"progress_stage": progress_stage})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_job_failed(client: Client, job_id: str, org_id: str, error: str) -> None:
    """Terminal failure transition: status -> failed, `error` set, `progress_stage` left
    as-is (design.md §4: the frontend renders the stage the job died at as `failed`,
    everything before it stays `complete`)."""
    (
        client.table("ingestion_jobs")
        .update({"status": "failed", "error": error})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )
