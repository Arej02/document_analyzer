"""Project resolve-or-create and read queries — design.md §4, specs.md FR (project identity across runs)."""

from supabase import Client

from .supabase_client import insert_scoped

_PROJECT_COLUMNS = "id, org_id, source_type, source_ref, latest_job_id, created_at, updated_at"
_JOB_COLUMNS = (
    "id, status, progress_stage, error, source_type, source_ref, created_at, updated_at, "
    "changed_file_count, unchanged_file_count, removed_file_count"
)


def resolve_or_create_project(client: Client, org_id: str, source_type: str, source_ref: str) -> str:
    """Matches an existing project by (org_id, source_type, source_ref), else creates one."""
    existing = (
        client.table("projects")
        .select("id")
        .eq("org_id", org_id)
        .eq("source_type", source_type)
        .eq("source_ref", source_ref)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"]

    created = insert_scoped(
        client,
        "projects",
        org_id,
        {"source_type": source_type, "source_ref": source_ref},
    )
    return created.data[0]["id"]


def _latest_job_status(client: Client, job_id: str | None) -> str | None:
    if not job_id:
        return None
    result = client.table("ingestion_jobs").select("status").eq("id", job_id).limit(1).execute()
    return result.data[0]["status"] if result.data else None


def list_projects(client: Client, org_id: str) -> list[dict]:
    """All projects for the org, each annotated with its latest job's status."""
    result = client.table("projects").select(_PROJECT_COLUMNS).eq("org_id", org_id).execute()
    projects = result.data
    for project in projects:
        project["latest_job_status"] = _latest_job_status(client, project.get("latest_job_id"))
    return projects


def get_project(client: Client, org_id: str, project_id: str) -> dict | None:
    """Single project with its full job history (most recent first), or None if not found/not this org's."""
    result = (
        client.table("projects")
        .select(_PROJECT_COLUMNS)
        .eq("org_id", org_id)
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    project = result.data[0]
    project["latest_job_status"] = _latest_job_status(client, project.get("latest_job_id"))

    jobs = (
        client.table("ingestion_jobs")
        .select(_JOB_COLUMNS)
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    )
    # Rebuilt explicitly rather than trusting `.select(_JOB_COLUMNS)` to have already
    # projected the row — correct against a real Postgrest backend, but tests/fakes.py's
    # FakeClient doesn't simulate column projection (same gap entities.py's _entity_stub
    # hit and fixed); a job row also carries github_token_ref, which must never reach a
    # response body regardless of what the client-side query claims to have selected.
    project["jobs"] = [
        {
            "id": job["id"],
            "status": job["status"],
            "progress_stage": job.get("progress_stage"),
            "error": job.get("error"),
            "source_type": job["source_type"],
            "source_ref": job["source_ref"],
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "changed_file_count": job.get("changed_file_count"),
            "unchanged_file_count": job.get("unchanged_file_count"),
            "removed_file_count": job.get("removed_file_count"),
        }
        for job in jobs.data
    ]
    return project
