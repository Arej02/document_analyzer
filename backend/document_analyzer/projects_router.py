"""GET /api/agent1/projects, GET /api/agent1/projects/{project_id},
POST /api/agent1/projects/{project_id}/reingest, and
DELETE /api/agent1/projects/{project_id} — design.md §4.

reingest_project_job (jobs.py) does the actual work here — it shares the
upload/GitHub job-row assembly POST /jobs already has, so it lives alongside
that rather than being duplicated in projects.py.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .auth import AuthContext, get_auth_context
from .jobs import reingest_project_job
from .projects import ProjectBusyError, delete_project, get_project, list_projects
from .shared import vault_secrets
from .supabase_client import get_service_role_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent1", tags=["projects"])


@router.get("/projects")
async def get_projects(auth: Annotated[AuthContext, Depends(get_auth_context)]):
    return list_projects(auth.client, auth.org_id)


@router.get("/projects/{project_id}")
async def get_project_detail(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    project = get_project(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/projects/{project_id}/reingest", status_code=202)
async def reingest_project(
    project_id: str, request: Request, auth: Annotated[AuthContext, Depends(get_auth_context)]
):
    return await reingest_project_job(project_id, request, auth)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project_endpoint(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    # Existence check on the caller's RLS-scoped client; the deletes need the service
    # role (no user-facing delete policies) and stay filtered by the caller's org_id.
    if get_project(auth.client, auth.org_id, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        token_refs = delete_project(get_service_role_client(), auth.org_id, project_id)
    except ProjectBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The project's rows are gone; its GitHub delivery token (Vault, design_agent3.md §15.7/§15.11) must not outlive it.
    # Best effort: an orphaned secret is harmless to the user, a failed request after a successful delete is not.
    for token_ref in token_refs:
        try:
            await vault_secrets.delete_secret(token_ref)
        except Exception:  # noqa: BLE001
            logger.warning("project %s deleted but vault secret %s could not be removed", project_id, token_ref)
    return Response(status_code=204)
