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

`persist_source_commit_sha`/`persist_source_snapshot_path` are Agent 3
prerequisites (design_agent3.md §13): the commit `worker/fetch.py` actually
cloned, and the object path of the post-boilerplate-strip source tarball
`worker/snapshot.py` archives, so Agent 3 can get at the exact source it's
translating. Both are null for archive uploads (no commit) / a job that
predates these columns.
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
        .in_("status", ["queued", "running"])  # never overwrite a cancelled job
        .execute()
    )


def persist_source_commit_sha(client: Client, job_id: str, org_id: str, source_commit_sha: str | None) -> None:
    """Records the commit `worker/fetch.py` cloned (None for archive uploads)."""
    (
        client.table("ingestion_jobs")
        .update({"source_commit_sha": source_commit_sha})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )


def persist_source_snapshot_path(client: Client, job_id: str, org_id: str, source_snapshot_path: str) -> None:
    """Records the `agent1-snapshots` object path `worker/snapshot.py` uploaded to."""
    (
        client.table("ingestion_jobs")
        .update({"source_snapshot_path": source_snapshot_path})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )


def persist_discovery_stats(client: Client, job_id: str, org_id: str, stats: dict) -> None:
    """Records the flat discovery map (design.md §2 `ingestion_jobs.discovery_stats`): loc,
    dependencies, language.<Name> and framework.<Name> counts, the same map the live
    `stats.updated` event carries."""
    (
        client.table("ingestion_jobs")
        .update({"discovery_stats": stats})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )


def persist_extraction_coverage(client: Client, job_id: str, org_id: str, coverage: dict) -> None:
    """Records what extraction and evidence actually covered (design.md §2
    `ingestion_jobs.extraction_coverage`): Graphify's per-status/per-language file counts
    and samples of files it didn't extract, files no extractor handles, and the evidence
    report. Bounded by the producers (samples are capped), never the full file list."""
    (
        client.table("ingestion_jobs")
        .update({"extraction_coverage": coverage})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )
