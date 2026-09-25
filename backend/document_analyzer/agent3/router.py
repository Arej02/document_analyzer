"""Agent 3 (Execute) API — design_agent3.md §9.

POST /api/agent3/plans/{plan_id}/executions creates an execution_jobs row
(status=queued) plus one pending unit_executions row per plan item, and
enqueues it on agent3_execution_jobs. Nothing here runs the pipeline inline —
see CLAUDE.md "Async by default".

Plan, project and source-job lookups go through the caller's RLS-scoped client,
same as Agents 1 and 2. The execution_jobs / unit_executions inserts go through
the service-role client: neither table has an authenticated insert policy
(design_agent3.md §2 "writes are service-role only"), and org_id is stamped
from the server-resolved AuthContext, never from the request.
"""

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError

from ..auth import AuthContext, get_auth_context
from ..queue import enqueue_execution_job
from ..shared.plan_staleness import is_stale, mark_stale_plans
from ..supabase_client import get_service_role_client, insert_many_scoped, insert_scoped
from .worker.sandbox import SandboxParams

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent3", tags=["agent3"])

_ACTIVE_STATUSES = ("queued", "running")

# Postgres unique_violation — execution_jobs_one_active_per_plan firing when two
# requests race past the active-job check below.
_UNIQUE_VIOLATION = "23505"

# PostgREST caps a response at 1000 rows; plan item reads page past it.
_PAGE_SIZE = 1000

_ACTIVE_JOB_DETAIL = "code generation is already queued or running for this plan"


def execution_params_from_env() -> dict:
    """design_agent3.md §11 defaults, frozen into execution_jobs.params at creation so
    later env changes never alter an existing job. AGENT3_SANDBOX_RUNTIME and
    AGENT3_REQUIRE_GVISOR are operational, not frozen."""
    sandbox = SandboxParams.from_env()
    return {
        "model_deployment": os.environ.get("AGENT3_MODEL_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
        "max_attempts": int(os.environ.get("AGENT3_MAX_ATTEMPTS", 3)),
        "max_context_chars": int(os.environ.get("AGENT3_MAX_CONTEXT_CHARS", 60000)),
        "sandbox_timeout_seconds": sandbox.timeout_seconds,
        "sandbox_memory_mb": sandbox.memory_mb,
        "runner_image": sandbox.image,
    }


@router.post("/plans/{plan_id}/executions", status_code=202)
async def create_execution(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan = _get_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")

    project = _get_project_row(auth.client, auth.org_id, plan["project_id"])
    latest_job_id = project.get("latest_job_id") if project else None
    mark_stale_plans(get_service_role_client(), auth.org_id, plan["project_id"], latest_job_id)
    if is_stale(plan["source_job_id"], latest_job_id) or plan["status"] == "stale":
        raise HTTPException(
            status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan"
        )
    if plan["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"only an approved plan can generate code; approve every phase first (status={plan['status']})",
        )

    source_job = _get_source_job(auth.client, auth.org_id, plan["source_job_id"])
    if source_job is None or not source_job.get("source_snapshot_path"):
        raise HTTPException(
            status_code=409,
            detail="this plan's ingestion run has no source snapshot; re-ingest this project to generate code",
        )

    items = _get_plan_items(auth.client, auth.org_id, plan_id)
    if not items:
        raise HTTPException(status_code=409, detail="plan has no units to generate")

    if _has_active_execution(auth.client, auth.org_id, plan_id):
        raise HTTPException(status_code=409, detail=_ACTIVE_JOB_DETAIL)

    service_client = get_service_role_client()
    row = {
        "project_id": plan["project_id"],
        "plan_id": plan_id,
        "source_job_id": plan["source_job_id"],
        "status": "queued",
        "params": execution_params_from_env(),
    }
    try:
        result = insert_scoped(service_client, "execution_jobs", auth.org_id, row)
    except APIError as exc:
        if exc.code == _UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail=_ACTIVE_JOB_DETAIL) from exc
        raise
    execution_job_id = result.data[0]["id"]

    unit_rows = [
        {
            "execution_job_id": execution_job_id,
            "cluster_id": item["cluster_id"],
            "phase_cluster_id": item["phase_cluster_id"],
            "phase_order": item["phase_order"],
            "unit_order": item["unit_order"],
            "status": "pending",
        }
        for item in items
    ]
    try:
        insert_many_scoped(service_client, "unit_executions", auth.org_id, unit_rows)
        await enqueue_execution_job(execution_job_id=execution_job_id, org_id=auth.org_id)
    except Exception:
        # A queued job nobody will run would block this plan forever
        # (execution_jobs_one_active_per_plan), so it's marked failed instead.
        logger.exception("could not start execution job %s", execution_job_id)
        _mark_not_started(service_client, auth.org_id, execution_job_id)
        raise

    return {"execution_job_id": execution_job_id, "status": "queued"}


def _mark_not_started(service_client, org_id: str, execution_job_id: str) -> None:
    try:
        (
            service_client.table("execution_jobs")
            .update({"status": "failed", "error": "could not start: creating units or enqueueing failed"})
            .eq("id", execution_job_id)
            .eq("org_id", org_id)
            .execute()
        )
    except Exception:  # noqa: BLE001 — the original error is the one to surface
        logger.exception("could not mark execution job %s failed", execution_job_id)


def _get_plan(client, org_id: str, plan_id: str) -> dict | None:
    result = (
        client.table("migration_plans")
        .select("id, project_id, source_job_id, status")
        .eq("org_id", org_id)
        .eq("id", plan_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_project_row(client, org_id: str, project_id: str) -> dict | None:
    result = (
        client.table("projects")
        .select("id, latest_job_id")
        .eq("org_id", org_id)
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_source_job(client, org_id: str, job_id: str) -> dict | None:
    result = (
        client.table("ingestion_jobs")
        .select("id, source_snapshot_path")
        .eq("org_id", org_id)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_plan_items(client, org_id: str, plan_id: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("migration_plan_items")
            .select("cluster_id, phase_cluster_id, phase_order, unit_order")
            .eq("org_id", org_id)
            .eq("plan_id", plan_id)
            .order("cluster_id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        ).data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _has_active_execution(client, org_id: str, plan_id: str) -> bool:
    rows = (
        client.table("execution_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("plan_id", plan_id)
        .in_("status", _ACTIVE_STATUSES)
        .execute()
    ).data
    return bool(rows)
