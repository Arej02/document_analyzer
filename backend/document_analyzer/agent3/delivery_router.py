"""Agent 3 GitHub delivery API — design_agent3.md §15.8, §15.10.

POST /api/agent3/executions/{execution_job_id}/deliver  -> 202, starts the delivery in a background task
GET  /api/agent3/executions/{execution_job_id}/delivery -> the execution's deliveries, newest first

The delivery itself (`delivery/deliver.py::run_delivery`) runs in a thread of this API process via FastAPI
`BackgroundTasks`, not a queue (§15.1: a bounded job). That is lost if the process restarts mid-run, so both
endpoints first sweep: a queued/running delivery whose heartbeat is older than 15 minutes is marked failed,
and starting again resumes from the branches and pull requests that already exist.

`POST` refuses (409) rather than starting something doomed: execution not finished or without output/manifest,
no GitHub connection, a stale plan, a token GitHub no longer accepts, `delete_branch_on_merge` now off, or a
delivery already queued/running (also the unique-index race).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from postgrest.exceptions import APIError

from ..auth import AuthContext, get_auth_context
from ..shared import vault_secrets
from ..shared.plan_staleness import is_stale, mark_stale_plans
from ..supabase_client import get_service_role_client, insert_scoped
from .delivery.deliver import OUTPUT_BUCKET, SETTINGS_PATH, run_delivery
from .delivery.github_api import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent3", tags=["agent3"])

_UNIQUE_VIOLATION = "23505"
_ACTIVE = ("queued", "running")
STALE_AFTER = timedelta(minutes=15)
_INTERRUPTED = "interrupted: the delivery stopped without finishing. Start it again; the branches and pull requests already created are reused."


def make_github_client(token: str) -> GitHubClient:
    """The seam tests replace to inject a fake transport."""
    return GitHubClient(token)


def _resolve_token_sync(token_ref: str) -> str | None:
    # Runs in the background task's worker thread, which has no event loop of its own.
    return asyncio.run(vault_secrets.resolve_secret(token_ref))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sweep_stale_deliveries(service, org_id: str, execution_job_id: str, *, now: datetime | None = None) -> None:
    """Marks this execution's queued/running deliveries with an old heartbeat as failed (§15.8)."""
    now = now or datetime.now(timezone.utc)
    rows = (
        service.table("deliveries")
        .select("id, heartbeat_at, created_at")
        .eq("org_id", org_id)
        .eq("execution_job_id", execution_job_id)
        .in_("status", list(_ACTIVE))
        .execute()
        .data
    )
    for row in rows:
        seen = _parse_time(row.get("heartbeat_at")) or _parse_time(row.get("created_at"))
        if seen is not None and now - seen > STALE_AFTER:
            (
                service.table("deliveries")
                .update({"status": "failed", "error": _INTERRUPTED, "updated_at": now.isoformat()})
                .eq("id", row["id"])
                .eq("org_id", org_id)
                .in_("status", list(_ACTIVE))
                .execute()
            )


def _get_execution(client, org_id: str, execution_job_id: str) -> dict | None:
    rows = (
        client.table("execution_jobs")
        .select("id, project_id, plan_id, status, output_zip_path")
        .eq("org_id", org_id)
        .eq("id", execution_job_id)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _manifest_exists(client, output_zip_path: str) -> bool:
    folder, _, _ = output_zip_path.rpartition("/")
    try:
        entries = client.storage.from_(OUTPUT_BUCKET).list(folder)
    except Exception:  # noqa: BLE001 — treat an unreadable listing as "not there"; the run re-checks anyway
        return False
    return any((entry.get("name") if isinstance(entry, dict) else entry) == "manifest.json" for entry in entries)


def _plan_is_stale(service, client, org_id: str, plan_id: str, project_id: str) -> bool:
    plan_rows = client.table("migration_plans").select("id, source_job_id, status").eq("org_id", org_id).eq("id", plan_id).limit(1).execute().data
    if not plan_rows:
        return False
    plan = plan_rows[0]
    project_rows = client.table("projects").select("id, latest_job_id").eq("org_id", org_id).eq("id", project_id).limit(1).execute().data
    latest = project_rows[0].get("latest_job_id") if project_rows else None
    mark_stale_plans(service, org_id, project_id, latest)
    return is_stale(plan["source_job_id"], latest) or plan["status"] == "stale"


def _check_github_still_ready(token: str, repo_full_name: str) -> None:
    """Runs in a thread. Raises HTTPException 409 with a message the user can act on."""
    client = make_github_client(token)
    try:
        info = client.get_repo(repo_full_name)
    except GitHubError as exc:
        if exc.kind in ("auth", "permission", "not_found"):
            raise HTTPException(status_code=409, detail="GitHub no longer accepts the stored token for this repository; reconnect it") from None
        raise HTTPException(status_code=502, detail="GitHub could not be reached; try again") from None
    finally:
        client.close()
    if info.archived or not info.can_push:
        raise HTTPException(status_code=409, detail="the stored token can no longer push to the repository; reconnect it")
    probe = make_github_client(token)
    try:
        probe.check_pull_request_access(repo_full_name)
    except GitHubError as exc:
        if exc.kind in ("permission", "not_found"):
            raise HTTPException(
                status_code=409,
                detail="the stored token cannot read pull requests on the repository; reconnect it with the Pull requests permission (read and write)",
            ) from None
        raise HTTPException(status_code=502, detail="GitHub could not be reached; try again") from None
    finally:
        probe.close()
    if info.delete_branch_on_merge is not True:
        raise HTTPException(
            status_code=409,
            detail=(
                "the repository no longer deletes head branches after a pull request is merged, so a stack of pull requests would not "
                f"reach the default branch. Turn on {SETTINGS_PATH} on GitHub, then try again."
            ),
        )


def _run_in_background(delivery_id: str, org_id: str) -> None:
    run_delivery(
        get_service_role_client(),
        delivery_id,
        org_id,
        github_factory=lambda token: make_github_client(token),
        resolve_token=_resolve_token_sync,
    )


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


@router.post("/executions/{execution_job_id}/deliver", status_code=202)
async def start_delivery(
    execution_job_id: str,
    background: BackgroundTasks,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
):
    execution = _get_execution(auth.client, auth.org_id, execution_job_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    if execution["status"] != "done" or not execution.get("output_zip_path"):
        raise HTTPException(status_code=409, detail="this execution has not finished generating code, so there is nothing to deliver yet")

    service = get_service_role_client()
    sweep_stale_deliveries(service, auth.org_id, execution_job_id)

    if not _manifest_exists(auth.client, execution["output_zip_path"]):
        raise HTTPException(
            status_code=409,
            detail="this execution finished before GitHub delivery existed and has no delivery manifest; run code generation again",
        )
    if _plan_is_stale(service, auth.client, auth.org_id, execution["plan_id"], execution["project_id"]):
        raise HTTPException(status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan")

    connection_rows = (
        service.table("project_github_connections")
        .select("repo_full_name, default_branch, token_ref")
        .eq("org_id", auth.org_id)
        .eq("project_id", execution["project_id"])
        .limit(1)
        .execute()
        .data
    )
    if not connection_rows:
        raise HTTPException(status_code=409, detail="connect a GitHub repository for this project first")
    connection = connection_rows[0]

    active = (
        service.table("deliveries")
        .select("id")
        .eq("org_id", auth.org_id)
        .eq("execution_job_id", execution_job_id)
        .in_("status", list(_ACTIVE))
        .limit(1)
        .execute()
        .data
    )
    if active:
        raise HTTPException(status_code=409, detail="a delivery to GitHub is already queued or running for this execution")

    token = await vault_secrets.resolve_secret(connection["token_ref"])
    if not token:
        raise HTTPException(status_code=409, detail="the stored GitHub token could not be read; reconnect the repository")
    await asyncio.to_thread(_check_github_still_ready, token, connection["repo_full_name"])

    try:
        row = insert_scoped(
            service,
            "deliveries",
            auth.org_id,
            {
                "execution_job_id": execution_job_id,
                "project_id": execution["project_id"],
                "repo_full_name": connection["repo_full_name"],
                "default_branch": connection["default_branch"],
                "status": "queued",
            },
        ).data[0]
    except APIError as exc:
        if exc.code == _UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail="a delivery to GitHub is already queued or running for this execution") from exc
        raise

    background.add_task(_run_in_background, row["id"], auth.org_id)
    return {"delivery_id": row["id"], "status": "queued"}


@router.get("/executions/{execution_job_id}/delivery")
async def get_deliveries(execution_job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if _get_execution(auth.client, auth.org_id, execution_job_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    sweep_stale_deliveries(get_service_role_client(), auth.org_id, execution_job_id)
    rows = (
        auth.client.table("deliveries")
        .select("id, status, error, repo_full_name, prs, root_matches_baseline, created_at")
        .eq("org_id", auth.org_id)
        .eq("execution_job_id", execution_job_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return {
        "deliveries": [
            {
                "delivery_id": row["id"],
                "status": row["status"],
                "error": row.get("error"),
                "repo_full_name": row["repo_full_name"],
                "prs": row.get("prs") or [],
                "root_matches_baseline": row.get("root_matches_baseline"),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ]
    }
