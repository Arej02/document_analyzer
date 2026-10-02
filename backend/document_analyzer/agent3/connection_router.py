"""Agent 3 GitHub connection API — design_agent3.md §15.7, §15.10, §15.13.

PUT/GET/DELETE /api/agent3/projects/{project_id}/github-connection. One connection per project: the
customer's separate target repo plus a write-scoped token, stored in Supabase Vault (never a column).

Rules this module enforces (each has a test):
- The token is accepted only in the PUT body, is never returned, logged, or echoed in an error. The body is
  read as a raw dict and validated by hand, because a framework validation error would repeat the offending
  input (the token) back in its 422 detail.
- Connect validates against GitHub BEFORE anything is stored: repo name, push permission, a repo with at most
  one branch, and `delete_branch_on_merge` on (§15.13). A failed check stores nothing, not even a Vault secret.
- Rotation creates the new secret, updates the row, and only then deletes the old secret, so a crash leaves an
  orphan secret and never a connection pointing at a deleted one.
- Connect and disconnect are refused (409) while a delivery for the project is queued or running.
- The caller's own project is looked up through their RLS-scoped client (404 for missing and other-org alike);
  the connection write goes through the service-role client, the table having no authenticated write policy.

GitHub calls are synchronous `httpx`, so they run in a worker thread and never block the event loop.
"""

import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from postgrest.exceptions import APIError

from ..auth import AuthContext, get_auth_context
from ..shared import vault_secrets
from ..supabase_client import get_service_role_client, insert_scoped
from .delivery.github_api import GitHubClient, GitHubError, redact, validate_repo_full_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent3", tags=["agent3"])

_UNIQUE_VIOLATION = "23505"
_ACTIVE_DELIVERY_STATUSES = ("queued", "running")
# Classic ghp_, fine-grained github_pat_, OAuth gho_ ... plus legacy 40-hex tokens: letters, digits, _ . -
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{20,255}$")
_SECRET_NAME_PREFIX = "agent3_delivery_token_"

_ACTIVE_DELIVERY_DETAIL = "a delivery to GitHub is queued or running for this project"
_SETTINGS_PATH = "Settings → General → Pull Requests → “Automatically delete head branches”"


def make_github_client(token: str) -> GitHubClient:
    """The seam tests replace to inject a fake transport."""
    return GitHubClient(token)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _project_exists(client, org_id: str, project_id: str) -> bool:
    result = client.table("projects").select("id").eq("org_id", org_id).eq("id", project_id).limit(1).execute()
    return bool(result.data)


def _has_active_delivery(client, org_id: str, project_id: str) -> bool:
    result = (
        client.table("deliveries")
        .select("id")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .in_("status", list(_ACTIVE_DELIVERY_STATUSES))
        .limit(1)
        .execute()
    )
    return bool(result.data)


def _get_connection_row(client, org_id: str, project_id: str, columns: str) -> dict | None:
    result = (
        client.table("project_github_connections")
        .select(columns)
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _parse_body(payload: Any) -> tuple[str, str]:
    """(repo_full_name, token), validated by hand so no error ever repeats the token."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object with repo_full_name and token")
    repo, token = payload.get("repo_full_name"), payload.get("token")
    if not isinstance(repo, str):
        raise HTTPException(status_code=422, detail="repo_full_name must be a string like owner/name")
    try:
        validate_repo_full_name(repo.strip())
    except GitHubError:
        raise HTTPException(status_code=422, detail="repo_full_name must look like owner/name") from None
    if not isinstance(token, str) or not _TOKEN_PATTERN.match(token.strip()):
        raise HTTPException(status_code=422, detail="token is missing or is not shaped like a GitHub access token")
    return repo.strip(), token.strip()


def _http_error(exc: GitHubError) -> HTTPException:
    """User-correctable GitHub failures are 422; GitHub being unavailable is 502."""
    detail = redact(str(exc))
    if exc.kind == "auth":
        return HTTPException(status_code=422, detail="GitHub rejected the token (it may be wrong, expired or revoked)")
    if exc.kind in ("permission", "not_found"):
        return HTTPException(
            status_code=422,
            detail="the repository was not found, or this token cannot access it (check the repository name and the token's repository access)",
        )
    if exc.kind == "invalid_input":
        return HTTPException(status_code=422, detail="repo_full_name must look like owner/name")
    if exc.kind == "redirect":
        return HTTPException(status_code=422, detail="GitHub redirected this repository name (it may have been renamed); use its current owner/name")
    return HTTPException(status_code=502, detail=f"GitHub could not be reached or answered unexpectedly; try again ({detail})")


def _validate_with_github(repo_full_name: str, token: str) -> tuple[str, list[str]]:
    """Runs the connect-time checks (§15.7, §15.13). Returns (default_branch, warnings) or raises HTTPException."""
    client = make_github_client(token)
    try:
        info = client.get_repo(repo_full_name)
        if info.archived:
            raise HTTPException(status_code=422, detail="the repository is archived, so nothing can be pushed to it")
        if not info.can_push:
            raise HTTPException(
                status_code=422,
                detail="this token cannot push to the repository (it needs write access to contents and pull requests)",
            )
        branches = client.list_branch_names(repo_full_name, limit=2)
        if len(branches) > 1:
            raise HTTPException(
                status_code=422,
                detail="the repository already has more than one branch; connect a new or empty repository, not an existing working one",
            )
        if info.delete_branch_on_merge is False:
            raise HTTPException(
                status_code=422,
                detail=(
                    "the repository does not delete head branches after a pull request is merged. Stacked pull requests only reach "
                    f"the default branch when merged branches are deleted. Turn on {_SETTINGS_PATH} in the repository settings on "
                    "GitHub, then connect again."
                ),
            )
        if info.delete_branch_on_merge is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "could not verify the repository's “Automatically delete head branches” setting with this token (GitHub did not "
                    f"report it). Use a token that can read the repository's settings, make sure {_SETTINGS_PATH} is on, and try again."
                ),
            )
        warnings = []
        if info.allow_merge_commit is False:
            warnings.append(
                "Merge commits are disabled on this repository. Squash or rebase merging rewrites a stacked pull request's commits "
                "and makes the next pull request show conflicts; “Create a merge commit” is recommended."
            )
        return info.default_branch, warnings
    except GitHubError as exc:
        raise _http_error(exc) from None
    finally:
        client.close()


def _secret_name(project_id: str) -> str:
    # Vault secret names are unique, and rotation needs two to coexist briefly.
    return f"{_SECRET_NAME_PREFIX}{project_id}_{secrets.token_hex(4)}"


async def _delete_secret_quietly(secret_id: str) -> None:
    try:
        await vault_secrets.delete_secret(secret_id)
    except Exception:  # noqa: BLE001 — an orphan secret is recoverable, a failed request is not worth it
        logger.warning("could not delete vault secret %s; it is now an orphan", secret_id)


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


@router.put("/projects/{project_id}/github-connection")
async def put_github_connection(
    project_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    payload: Annotated[Any, Body()] = None,
):
    if not _project_exists(auth.client, auth.org_id, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    repo_full_name, token = _parse_body(payload)

    service = get_service_role_client()
    if _has_active_delivery(service, auth.org_id, project_id):
        raise HTTPException(status_code=409, detail=_ACTIVE_DELIVERY_DETAIL)

    default_branch, warnings = await asyncio.to_thread(_validate_with_github, repo_full_name, token)

    # Everything above stored nothing. From here a secret exists, so every failure path removes it.
    new_ref = await vault_secrets.store_secret(
        _secret_name(project_id), f"GitHub delivery token for project {project_id}", token
    )
    try:
        existing = _get_connection_row(service, auth.org_id, project_id, "token_ref")
        row = {
            "repo_full_name": repo_full_name,
            "default_branch": default_branch,
            "token_ref": new_ref,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        old_ref = None
        if existing is not None:
            old_ref = existing["token_ref"]
            service.table("project_github_connections").update(row).eq("project_id", project_id).eq("org_id", auth.org_id).execute()
        else:
            try:
                insert_scoped(service, "project_github_connections", auth.org_id, {"project_id": project_id, "created_by": auth.user_id, **row})
            except APIError as exc:
                if exc.code != _UNIQUE_VIOLATION:
                    raise
                # A concurrent PUT created it first: treat ours as the rotation.
                raced = _get_connection_row(service, auth.org_id, project_id, "token_ref")
                old_ref = raced["token_ref"] if raced else None
                service.table("project_github_connections").update(row).eq("project_id", project_id).eq("org_id", auth.org_id).execute()
    except Exception:
        await _delete_secret_quietly(new_ref)
        raise

    if old_ref and old_ref != new_ref:
        await _delete_secret_quietly(old_ref)

    return {"connected": True, "repo_full_name": repo_full_name, "default_branch": default_branch, "warnings": warnings}


@router.get("/projects/{project_id}/github-connection")
async def get_github_connection(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if not _project_exists(auth.client, auth.org_id, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    row = _get_connection_row(auth.client, auth.org_id, project_id, "repo_full_name, default_branch, created_at")
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "repo_full_name": row["repo_full_name"],
        "default_branch": row["default_branch"],
        "created_at": row["created_at"],
    }


@router.delete("/projects/{project_id}/github-connection", status_code=204)
async def delete_github_connection(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if not _project_exists(auth.client, auth.org_id, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    service = get_service_role_client()
    if _has_active_delivery(service, auth.org_id, project_id):
        raise HTTPException(status_code=409, detail=_ACTIVE_DELIVERY_DETAIL)

    existing = _get_connection_row(service, auth.org_id, project_id, "token_ref")
    if existing is not None:
        # Row first, secret second: the connection never points at a deleted secret.
        service.table("project_github_connections").delete().eq("project_id", project_id).eq("org_id", auth.org_id).execute()
        await _delete_secret_quietly(existing["token_ref"])
    return Response(status_code=204)
