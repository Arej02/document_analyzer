"""POST /api/agent1/jobs, GET /api/agent1/jobs/{job_id},
GET /api/agent1/jobs/{job_id}/summary, GET /api/agent1/jobs/{job_id}/entities, and
GET /api/agent1/jobs/{job_id}/entities/{entity_id} — design.md §4.

POST resolves/creates the project row, creates the ingestion_jobs row
(status=queued), and enqueues the job. Nothing here runs extraction/
summarization inline — see CLAUDE.md "Async by default".

GET /jobs/{job_id} is the status-polling endpoint the frontend's job status
page needs (tasks.md Phase 2 "Frontend sync — Extraction progress") —
design.md §4 already specified its contract, but no backend task ever existed
for actually building it (a gap discovered while wiring up that frontend
task, not a new design decision), so it's added here alongside the endpoint
that already lives in this file for the same resource.

GET /jobs/{job_id}/summary is the same situation, discovered the same way
while wiring up tasks.md Phase 4 "Frontend sync — Results view"'s first task:
design.md §4 already specified this contract too, but no backend task for it
existed anywhere in Phases 0-4. Only valid once the job's status is `done`
(design.md §4) — a 409 otherwise, distinct from the 404 for a missing/other-
org job — since the repo-level summary and entity counts it reads
(`knowledge_summaries` level=`repo`, `extracted_entities`) aren't meaningful,
and may not exist at all, before then.

GET /jobs/{job_id}/entities is the same gap pattern again, discovered while
wiring up tasks.md Phase 4 "Frontend sync — Results view"'s third task:
design.md §4 already named this endpoint (paginated entities + `entity_type`/
`risk_level` filters) but never pinned down a request/response shape, so
entities.py's `list_job_entities` docstring is where that shape actually gets
decided — mirrored in design.md §4. Unlike /summary, this one doesn't gate on
job status: the knowledge base is meant to be browsable incrementally as
entities/summaries land, not just once a job is fully `done`.

GET /jobs/{job_id}/entities/{entity_id} is the same gap pattern a fourth
time, and the last one in Phase 4: design.md §4 named it ("raw facts,
summary, grounding status, and its edges (both directions)") without a
shape, so entities.py's `get_entity_detail` docstring decides it, mirrored in
design.md §4. Same no-status-gate reasoning as the list endpoint.

reingest_project_job (used by projects_router.py's
`POST /api/agent1/projects/{project_id}/reingest`) lives here rather than in
projects.py because it shares the upload/GitHub job-row assembly this file
already has for `POST /jobs` — design.md §4 named the endpoint's behavior but
this is where its request/response shape is pinned down, tasks.md Phase 5.

GET /jobs/{job_id} now also returns changed_file_count/unchanged_file_count/
removed_file_count (worker/diff.py's per-job diff, tasks.md Phase 5) — the
count the "distinguish a fresh ingestion from a cached/incremental one"
frontend task needed exposed, and the last piece of design.md §4's contract
that wasn't already live before this pass.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from .auth import AuthContext, get_auth_context
from .entities import DEFAULT_LIMIT, MAX_LIMIT, get_entity_detail, list_job_entities
from .github_token import store_github_token
from .projects import get_project, resolve_or_create_project
from .queue import enqueue_ingestion_job
from .storage import store_upload
from .supabase_client import insert_scoped

router = APIRouter(prefix="/api/agent1", tags=["jobs"])

_JOB_STATUS_COLUMNS = (
    "id, status, progress_stage, error, changed_file_count, unchanged_file_count, removed_file_count"
)


class GithubJobRequest(BaseModel):
    source_type: Literal["github"]
    repo_url: str
    access_token: str | None = None


@router.post("/jobs", status_code=202)
async def create_job(request: Request, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        job_row = await _build_upload_job_row(request, auth)
    elif content_type.startswith("application/json"):
        job_row = await _build_github_job_row(request, auth)
    else:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be multipart/form-data (upload) or application/json (GitHub)",
        )

    result = insert_scoped(auth.client, "ingestion_jobs", auth.org_id, job_row)
    job_id = result.data[0]["id"]

    await enqueue_ingestion_job(job_id=job_id, org_id=auth.org_id)

    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    job = get_job_status(auth.client, auth.org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/jobs/{job_id}/summary")
async def get_job_summary_endpoint(job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    job = get_job_status(auth.client, auth.org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job['status']})")
    return get_job_summary(auth.client, auth.org_id, job_id)


@router.get("/jobs/{job_id}/entities")
async def get_job_entities(
    job_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    entity_type: str | None = None,
    risk_level: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    job = get_job_status(auth.client, auth.org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return list_job_entities(
        auth.client, auth.org_id, job_id, entity_type=entity_type, risk_level=risk_level, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}/entities/{entity_id}")
async def get_job_entity_detail(
    job_id: str, entity_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]
):
    job = get_job_status(auth.client, auth.org_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    entity = get_entity_detail(auth.client, auth.org_id, job_id, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return entity


def get_job_status(client, org_id: str, job_id: str) -> dict | None:
    """Single job's poll-able status, or None if not found/not this org's job.

    changed_file_count/unchanged_file_count/removed_file_count (design.md §4, tasks.md
    Phase 5) are null until worker/diff.py runs for this job, same as progress_stage/
    error being null before their own fields are set — not a separate gate here."""
    result = (
        client.table("ingestion_jobs")
        .select(_JOB_STATUS_COLUMNS)
        .eq("org_id", org_id)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    return {
        "job_id": row["id"],
        "status": row["status"],
        "progress_stage": row.get("progress_stage"),
        "error": row.get("error"),
        "changed_file_count": row.get("changed_file_count"),
        "unchanged_file_count": row.get("unchanged_file_count"),
        "removed_file_count": row.get("removed_file_count"),
    }


def get_job_summary(client, org_id: str, job_id: str) -> dict:
    """Repo-level summary + counts for a (already-confirmed-`done`) job, per design.md
    §4's documented response shape. Callers must check the job exists and is `done`
    first (get_job_summary_endpoint does) — this assumes both and doesn't re-check.

    `entity_counts` groups extracted_entities by entity_type. `repo_summary`/`risk_level`
    come from the single level="repo" knowledge_summaries row for this job, or None if
    one was never persisted (e.g. an empty/all-boilerplate repo — "done" doesn't
    guarantee a non-empty repo summary exists). `unverified_count` counts every
    knowledge_summaries row (any level) with grounding_status="unverified" — the
    actionable "needs manual review" count the next tasks.md task surfaces in the UI.
    """
    entities = (
        client.table("extracted_entities").select("entity_type").eq("org_id", org_id).eq("job_id", job_id).execute()
    ).data
    entity_counts: dict[str, int] = {}
    for row in entities:
        entity_counts[row["entity_type"]] = entity_counts.get(row["entity_type"], 0) + 1

    repo_rows = (
        client.table("knowledge_summaries")
        .select("summary_text, risk_level")
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("level", "repo")
        .limit(1)
        .execute()
    ).data
    repo_summary = repo_rows[0]["summary_text"] if repo_rows else None
    risk_level = repo_rows[0]["risk_level"] if repo_rows else None

    unverified = (
        client.table("knowledge_summaries")
        .select("id")
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("grounding_status", "unverified")
        .execute()
    ).data

    return {
        "repo_summary": repo_summary,
        "risk_level": risk_level,
        "entity_counts": entity_counts,
        "unverified_count": len(unverified),
    }


async def _build_upload_job_row(request: Request, auth: AuthContext) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(status_code=400, detail="file field is required for upload jobs")

    # Pre-generated so the storage path and the job row agree on the same id.
    job_id = str(uuid.uuid4())
    source_ref = await store_upload(auth.client, auth.org_id, job_id, upload)
    project_id = resolve_or_create_project(auth.client, auth.org_id, "upload", source_ref)

    return {
        "id": job_id,
        "project_id": project_id,
        "source_type": "upload",
        "source_ref": source_ref,
        "status": "queued",
    }


async def _build_github_job_row(request: Request, auth: AuthContext) -> dict:
    body = await request.json()
    try:
        github_request = GithubJobRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    project_id = resolve_or_create_project(auth.client, auth.org_id, "github", github_request.repo_url)

    # Pre-generated so the secret can be scoped to this job's id from creation.
    job_id = str(uuid.uuid4())
    github_token_ref = None
    if github_request.access_token:
        github_token_ref = await store_github_token(job_id, github_request.access_token)

    return {
        "id": job_id,
        "project_id": project_id,
        "source_type": "github",
        "source_ref": github_request.repo_url,
        "github_token_ref": github_token_ref,
        "status": "queued",
    }


async def reingest_project_job(project_id: str, request: Request, auth: AuthContext) -> dict:
    """Creates a new ingestion_jobs row against an existing project_id, re-pulling the
    same source — design.md §4 `POST /api/agent1/projects/{project_id}/reingest`.

    A github-sourced project re-pulls `project["source_ref"]` (the repo URL) fresh; an
    access_token for a private repo can optionally be supplied again in a JSON body
    (`{"access_token": "..."}`) — github_token_ref is per-job (github_token.py), so a
    prior job's token, if any, is never reused. An upload-sourced project has no
    stored original content to "re-pull" — it requires a new archive in the request,
    same multipart shape as `POST /jobs`.

    Scope note (tasks.md Phase 5): this only creates the job row and enqueues it, same
    as `POST /jobs`. Diffing against the previous job by content_hash, confirming
    summary_cache reuse, and updating projects.latest_job_id once the new job reaches
    `done` are separate, not-yet-built tasks — this doesn't do any of that yet.
    """
    project = get_project(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    if project["source_type"] == "github":
        job_row = await _build_github_reingest_job_row(request, project)
    elif project["source_type"] == "upload":
        job_row = await _build_upload_reingest_job_row(request, auth, project_id)
    else:
        raise HTTPException(status_code=500, detail=f"unknown source_type: {project['source_type']}")

    result = insert_scoped(auth.client, "ingestion_jobs", auth.org_id, job_row)
    job_id = result.data[0]["id"]

    await enqueue_ingestion_job(job_id=job_id, org_id=auth.org_id)

    return {"job_id": job_id, "status": "queued"}


async def _build_upload_reingest_job_row(request: Request, auth: AuthContext, project_id: str) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(status_code=400, detail="file field is required to reingest an upload-sourced project")

    job_id = str(uuid.uuid4())
    source_ref = await store_upload(auth.client, auth.org_id, job_id, upload)

    return {
        "id": job_id,
        "project_id": project_id,
        "source_type": "upload",
        "source_ref": source_ref,
        "status": "queued",
    }


async def _build_github_reingest_job_row(request: Request, project: dict) -> dict:
    access_token = None
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = await request.json()
        if isinstance(body, dict):
            access_token = body.get("access_token")

    job_id = str(uuid.uuid4())
    github_token_ref = None
    if access_token:
        github_token_ref = await store_github_token(job_id, access_token)

    return {
        "id": job_id,
        "project_id": project["id"],
        "source_type": "github",
        "source_ref": project["source_ref"],
        "github_token_ref": github_token_ref,
        "status": "queued",
    }
