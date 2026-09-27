"""Project resolve-or-create and read queries — design.md §4, specs.md FR (project identity across runs)."""

import logging

from supabase import Client

from .storage import BUCKET
from .supabase_client import insert_scoped
from .worker.snapshot import SNAPSHOT_BUCKET

logger = logging.getLogger(__name__)

# Agent 3's private output bucket (design_agent3.md §2), objects under
# `{org_id}/{execution_job_id}/...`. Named here rather than imported so Agent 1
# doesn't import from Agent 3.
AGENT3_OUTPUT_BUCKET = "agent3-output"

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


# PostgREST sends `in` filters in the URL, so id lists are chunked to keep it short.
_ID_CHUNK = 200
# Default PostgREST page size cap (max rows per select).
_PAGE_SIZE = 1000
_ACTIVE_STATUSES = ("queued", "running")


class ProjectBusyError(Exception):
    """The project still has an ingestion, planning or execution job in flight."""


def _chunks(ids: list[str]):
    for i in range(0, len(ids), _ID_CHUNK):
        yield ids[i : i + _ID_CHUNK]


def _ids(client: Client, table: str, org_id: str, column: str, value: str) -> list[str]:
    ids: list[str] = []
    start = 0
    while True:
        page = (
            client.table(table)
            .select("id")
            .eq("org_id", org_id)
            .eq(column, value)
            .order("id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
            .data
        )
        ids.extend(row["id"] for row in page)
        if len(page) < _PAGE_SIZE:
            return ids
        start += _PAGE_SIZE


def _delete_in(client: Client, table: str, org_id: str, column: str, ids: list[str]) -> None:
    for chunk in _chunks(ids):
        client.table(table).delete().eq("org_id", org_id).in_(column, chunk).execute()


def delete_project(client: Client, org_id: str, project_id: str) -> None:
    """Deletes a project and everything derived from it — design.md §4
    `DELETE /api/agent1/projects/{project_id}`. `client` is the service-role client;
    every query is filtered by `org_id`. Children go before parents because Agent 1's
    FKs don't cascade, and every step is a filtered delete, so a partial failure can be
    retried. Raises ProjectBusyError while a job for the project is queued/running.

    Agent 3 (design_agent3.md §13): also removes each job's `agent1-snapshots`
    source tarball, every execution job of the project with its unit rows, and
    their `agent3-output` objects, and refuses while an execution job is active."""
    jobs = (
        client.table("ingestion_jobs")
        .select("id, status, source_type, source_ref, source_snapshot_path")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .execute()
        .data
    )
    planning_jobs = (
        client.table("planning_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .execute()
        .data
    )
    execution_jobs = (
        client.table("execution_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .execute()
        .data
    )
    if any(row["status"] in _ACTIVE_STATUSES for row in jobs + planning_jobs + execution_jobs):
        raise ProjectBusyError(
            "an ingestion, planning or code generation job for this project is still queued or running"
        )

    # Agent 3 first: unit_executions reference the plan's clusters and execution_jobs
    # reference its plans, and neither cascades from Agent 2's rows.
    execution_job_ids = [row["id"] for row in execution_jobs]
    _delete_in(client, "unit_executions", org_id, "execution_job_id", execution_job_ids)
    client.table("execution_jobs").delete().eq("org_id", org_id).eq("project_id", project_id).execute()

    # Agent 2: plan rows first (no cascade from plans), then planning jobs, whose FKs
    # cascade to clusters, cluster_members, cluster_reports and cluster_edges.
    plan_ids = _ids(client, "migration_plans", org_id, "project_id", project_id)
    for table in ("plan_overrides", "phase_approvals", "migration_plan_items"):
        _delete_in(client, table, org_id, "plan_id", plan_ids)
    client.table("migration_plans").delete().eq("org_id", org_id).eq("project_id", project_id).execute()
    client.table("planning_jobs").delete().eq("org_id", org_id).eq("project_id", project_id).execute()

    # Agent 1. latest_job_id points at one of the jobs being deleted.
    client.table("projects").update({"latest_job_id": None}).eq("org_id", org_id).eq("id", project_id).execute()
    for job in jobs:
        job_id = job["id"]
        # summary_cache is org-wide and has no job_id: drop the rows pointing at this
        # job's summaries (their only link), so the cache never dangles.
        summary_ids = _ids(client, "knowledge_summaries", org_id, "job_id", job_id)
        _delete_in(client, "summary_cache", org_id, "summary_id", summary_ids)
        for table in ("secret_findings", "knowledge_summaries", "entity_edges", "extracted_entities", "filtered_files"):
            client.table(table).delete().eq("org_id", org_id).eq("job_id", job_id).execute()
    client.table("ingestion_jobs").delete().eq("org_id", org_id).eq("project_id", project_id).execute()
    client.table("projects").delete().eq("org_id", org_id).eq("id", project_id).execute()

    upload_paths = [job["source_ref"] for job in jobs if job["source_type"] == "upload"]
    if upload_paths:
        try:
            client.storage.from_(BUCKET).remove(upload_paths)
        except Exception:  # noqa: BLE001 — the rows are gone; an orphaned archive isn't worth failing over
            logger.warning("project %s deleted but its uploaded archives could not be removed", project_id, exc_info=True)

    snapshot_paths = [job["source_snapshot_path"] for job in jobs if job.get("source_snapshot_path")]
    if snapshot_paths:
        try:
            client.storage.from_(SNAPSHOT_BUCKET).remove(snapshot_paths)
        except Exception:  # noqa: BLE001 — the rows are gone; an orphaned snapshot isn't worth failing over
            logger.warning("project %s deleted but its source snapshots could not be removed", project_id, exc_info=True)

    if execution_job_ids:
        try:
            bucket = client.storage.from_(AGENT3_OUTPUT_BUCKET)
            output_paths = [
                path for job_id in execution_job_ids for path in _list_objects(bucket, f"{org_id}/{job_id}")
            ]
            for chunk in _chunks(output_paths):
                bucket.remove(chunk)
        except Exception:  # noqa: BLE001 — the rows are gone; orphaned output isn't worth failing over
            logger.warning("project %s deleted but its generated code could not be removed", project_id, exc_info=True)


def _list_objects(bucket, prefix: str) -> list[str]:
    """Every object path under `prefix`, recursively. Storage lists one folder level at
    a time, and a folder comes back as an entry with no `id`."""
    paths: list[str] = []
    offset = 0
    while True:
        entries = bucket.list(prefix, {"limit": _PAGE_SIZE, "offset": offset})
        for entry in entries:
            path = f"{prefix}/{entry['name']}"
            if entry.get("id") is None:
                paths.extend(_list_objects(bucket, path))
            else:
                paths.append(path)
        if len(entries) < _PAGE_SIZE:
            return paths
        offset += _PAGE_SIZE
