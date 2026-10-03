"""Assessment Report API — design_report.md §9, §9.1.

POST /api/projects/{project_id}/reports creates a report_generation_jobs row (status=queued)
and enqueues it on report_generation_jobs. Nothing here builds a report inline (CLAUDE.md
"Async by default"). The GET endpoints list reports, read one, and poll a job's events.

Reads go through the caller's RLS-scoped client. The job insert goes through the service-role
client (report_generation_jobs has no authenticated insert policy), with org_id stamped from
the server-resolved AuthContext, never from the request. The report only reads Agents 1-3's
tables: it never calls `mark_stale_plans`, so "out of date" is computed on read and nothing of
Agent 1 or 2 is written (design_report.md §10).
"""

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from postgrest.exceptions import APIError
from pydantic import BaseModel

from ..auth import AuthContext, get_auth_context
from ..jobs import EVENTS_DEFAULT_LIMIT, EVENTS_MAX_LIMIT
from ..queue import enqueue_report_job
from ..shared.cost_access import get_cost_operator
from .config import load_config
from .pdf import DEFAULT_RENDERER_IMAGE  # frozen into params at creation so a job keeps its value
from .worker import artifacts
from ..shared.plan_staleness import is_stale
from ..supabase_client import get_service_role_client, insert_scoped
from ..worker.ground_retry import GROUNDING_RETRY_CAP

logger = logging.getLogger(__name__)

router = APIRouter(tags=["report"])

_ACTIVE_STATUSES = ("queued", "running")
_ELIGIBLE_PLAN_STATUSES = ("draft", "in_review", "approved")
_DEAD_PLAN_STATUSES = ("stale", "rejected")

# Postgres unique_violation: report_generation_jobs_one_active_per_project_tier firing when two
# requests race past the active-job check.
_UNIQUE_VIOLATION = "23505"
_ACTIVE_JOB_DETAIL = "a {tier} report is already queued or running for this project"


_EVENT_COLUMNS = "id, event_type, stage, message, data, created_at"
_LIST_COLUMNS = (
    "id, tier, plan_id, plan_version, plan_status_at_generation, exec_summary_status, pdf_status, "
    "source_job_id, created_at"
)
_DETAIL_COLUMNS = (
    "id, project_id, generation_job_id, tier, source_job_id, plan_id, plan_version, plan_status_at_generation, "
    "report_json, schema_version, template_version, estimator_version, exec_summary_status, pdf_status, "
    "html_path, created_at"
)


class CreateReport(BaseModel):
    tier: Literal["xray", "full"]


def report_params_from_env() -> dict:
    """design_report.md §12 values, frozen into report_generation_jobs.params at creation so
    later env changes never alter an existing job. REPORT_SANDBOX_RUNTIME and the queue
    visibility timeout are operational, not frozen."""
    return {
        "model_deployment": os.environ.get("REPORT_MODEL_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
        "max_grounding_retries": int(os.environ.get("REPORT_MAX_GROUNDING_RETRIES", GROUNDING_RETRY_CAP)),
        "pdf_timeout_seconds": int(os.environ.get("REPORT_PDF_TIMEOUT_SECONDS", 120)),
        "pdf_memory_mb": int(os.environ.get("REPORT_PDF_MEMORY_MB", 1024)),
        "renderer_image": os.environ.get("REPORT_RENDERER_IMAGE", DEFAULT_RENDERER_IMAGE),
    }


def _first(query) -> dict | None:
    rows = query.limit(1).execute().data
    return rows[0] if rows else None


def _get_project(client, org_id: str, project_id: str) -> dict | None:
    return _first(client.table("projects").select("id, latest_job_id").eq("org_id", org_id).eq("id", project_id))


def _plan_statuses(client, org_id: str, plan_ids: list[str]) -> dict[str, str]:
    """Current status of the pinned plans: one select, however many reports there are."""
    if not plan_ids:
        return {}
    rows = client.table("migration_plans").select("id, status").eq("org_id", org_id).in_("id", plan_ids).execute().data
    return {row["id"]: row["status"] for row in rows}


def _out_of_date(report: dict, latest_job_id: str | None, plan_status: str | None) -> bool:
    return is_stale(report["source_job_id"], latest_job_id) or plan_status in _DEAD_PLAN_STATUSES


def _pick_plan(plans: list[dict]) -> dict:
    """approved first, then the highest version, then the newest."""
    return max(plans, key=lambda p: (p["status"] == "approved", p["version"], str(p["created_at"])))


def _eligibility(client, org_id: str, project: dict) -> tuple[dict, dict | None]:
    """`({"xray": {...}, "full": {...}}, plan)`: whether each tier can be generated now and why
    not, plus the plan a full report would be pinned to."""
    latest_job_id = project.get("latest_job_id")
    source = (
        _first(client.table("ingestion_jobs").select("id, status").eq("org_id", org_id).eq("id", latest_job_id))
        if latest_job_id
        else None
    )
    if source is None or source["status"] != "done":
        reason = "no completed ingestion yet"
        return {"xray": {"eligible": False, "reason": reason}, "full": {"eligible": False, "reason": reason}}, None

    plans = (
        client.table("migration_plans")
        .select("id, status, version, source_job_id, created_at")
        .eq("org_id", org_id)
        .eq("project_id", project["id"])
        .execute()
        .data
    )
    current = [p for p in plans if p["source_job_id"] == latest_job_id]
    usable = [p for p in current if p["status"] in _ELIGIBLE_PLAN_STATUSES]
    if usable:
        plan = _pick_plan(usable)
        full = {"eligible": True, "reason": None}
    else:
        plan = None
        reason = (
            "the plan for the latest ingestion is stale or rejected; generate a new plan"
            if current
            else "no migration plan for the latest ingestion yet"
        )
        full = {"eligible": False, "reason": reason}

    active = {
        row["tier"]
        for row in client.table("report_generation_jobs")
        .select("tier, status")
        .eq("org_id", org_id)
        .eq("project_id", project["id"])
        .in_("status", _ACTIVE_STATUSES)
        .execute()
        .data
    }
    xray = {"eligible": True, "reason": None}
    for tier, state in (("xray", xray), ("full", full)):
        if tier in active and state["eligible"]:
            state.update(eligible=False, reason=_ACTIVE_JOB_DETAIL.format(tier=tier))
    return {"xray": xray, "full": full}, plan


@router.post("/api/projects/{project_id}/reports", status_code=202)
async def create_report(project_id: str, body: CreateReport, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    project = _get_project(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    eligibility, plan = _eligibility(auth.client, auth.org_id, project)
    state = eligibility[body.tier]
    if not state["eligible"]:
        raise HTTPException(status_code=409, detail=state["reason"])

    row = {
        "project_id": project_id,
        "tier": body.tier,
        "source_job_id": project["latest_job_id"],
        "plan_id": plan["id"] if body.tier == "full" else None,
        "plan_version": plan["version"] if body.tier == "full" else None,
        "status": "queued",
        "params": report_params_from_env(),
        "created_by": auth.user_id,
    }
    service_client = get_service_role_client()
    try:
        result = insert_scoped(service_client, "report_generation_jobs", auth.org_id, row)
    except APIError as exc:
        if exc.code == _UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail=_ACTIVE_JOB_DETAIL.format(tier=body.tier)) from exc
        raise
    report_job_id = result.data[0]["id"]

    try:
        await enqueue_report_job(report_job_id=report_job_id, org_id=auth.org_id)
    except Exception:
        # A queued job nobody will run would block this project and tier forever
        # (the one-active unique index), so it's marked failed instead.
        logger.exception("could not enqueue report job %s", report_job_id)
        _mark_not_started(service_client, auth.org_id, report_job_id)
        raise

    return {"report_job_id": report_job_id, "status": "queued"}


def _mark_not_started(service_client, org_id: str, report_job_id: str) -> None:
    try:
        (
            service_client.table("report_generation_jobs")
            .update({"status": "failed", "error": "could not start: enqueueing failed"})
            .eq("id", report_job_id)
            .eq("org_id", org_id)
            .execute()
        )
    except Exception:  # noqa: BLE001 — the original error is the one to surface
        logger.exception("could not mark report job %s failed", report_job_id)


@router.get("/api/projects/{project_id}/reports")
async def list_reports(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    project = _get_project(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    rows = (
        auth.client.table("assessment_reports")
        .select(_LIST_COLUMNS)
        .eq("org_id", auth.org_id)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    plan_status = _plan_statuses(auth.client, auth.org_id, sorted({r["plan_id"] for r in rows if r.get("plan_id")}))
    reports = [
        {
            "id": r["id"],
            "tier": r["tier"],
            "plan_id": r.get("plan_id"),
            "plan_version": r.get("plan_version"),
            "plan_status_at_generation": r.get("plan_status_at_generation"),
            "plan_status": plan_status.get(r.get("plan_id")),
            "exec_summary_status": r["exec_summary_status"],
            "pdf_status": r["pdf_status"],
            "created_at": r["created_at"],
            "out_of_date": _out_of_date(r, project.get("latest_job_id"), plan_status.get(r.get("plan_id"))),
        }
        for r in rows
    ]

    active = (
        auth.client.table("report_generation_jobs")
        .select("id, tier, status, progress_stage, created_at")
        .eq("org_id", auth.org_id)
        .eq("project_id", project_id)
        .in_("status", _ACTIVE_STATUSES)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    eligibility, _ = _eligibility(auth.client, auth.org_id, project)
    return {
        "reports": reports,
        "active_jobs": [
            {k: j.get(k) for k in ("id", "tier", "status", "progress_stage", "created_at")} for j in active
        ],
        "eligibility": eligibility,
    }


@router.get("/api/reports/jobs/{job_id}/events")
async def get_report_job_events(
    job_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=EVENTS_MAX_LIMIT)] = EVENTS_DEFAULT_LIMIT,
):
    job = _first(
        auth.client.table("report_generation_jobs")
        .select("id, tier, status, progress_stage, error")
        .eq("org_id", auth.org_id)
        .eq("id", job_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="report job not found")

    raw_events = (
        auth.client.table("workflow_events")
        .select(_EVENT_COLUMNS)
        .eq("org_id", auth.org_id)
        .eq("workflow_kind", "report_generation")
        .eq("workflow_id", job_id)
        .gt("id", after)
        .order("id")
        .limit(limit)
        .execute()
        .data
    )
    # Rebuilt explicitly so org_id can never reach a response body, whatever the select returned.
    events = [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "stage": row.get("stage"),
            "message": row.get("message"),
            "data": row.get("data", {}),
            "created_at": row.get("created_at"),
        }
        for row in raw_events
    ]
    report = _first(
        auth.client.table("assessment_reports").select("id").eq("org_id", auth.org_id).eq("generation_job_id", job_id)
    )
    return {
        "job_id": job["id"],
        "tier": job["tier"],
        "status": job["status"],
        "progress_stage": job.get("progress_stage"),
        "error": job.get("error"),
        "report_id": report["id"] if report else None,
        "events": events,
        "next_cursor": events[-1]["id"] if events else after,
        "server_time": datetime.now(UTC).isoformat(),
    }


@router.get("/api/reports/{report_id}")
async def get_report(report_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    row = _first(auth.client.table("assessment_reports").select(_DETAIL_COLUMNS).eq("org_id", auth.org_id).eq("id", report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    project = _get_project(auth.client, auth.org_id, row["project_id"])
    plan_status = _plan_statuses(auth.client, auth.org_id, [row["plan_id"]] if row.get("plan_id") else []).get(row.get("plan_id"))
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "generation_job_id": row["generation_job_id"],
        "tier": row["tier"],
        "source_job_id": row["source_job_id"],
        "plan_id": row.get("plan_id"),
        "plan_version": row.get("plan_version"),
        "plan_status_at_generation": row.get("plan_status_at_generation"),
        "plan_status": plan_status,
        "report_json": row["report_json"],
        "schema_version": row["schema_version"],
        "template_version": row["template_version"],
        "estimator_version": row["estimator_version"],
        "exec_summary_status": row["exec_summary_status"],
        "pdf_status": row["pdf_status"],
        "has_html": bool(row.get("html_path")),
        "created_at": row["created_at"],
        "out_of_date": _out_of_date(row, project.get("latest_job_id") if project else None, plan_status),
    }


# --- stored artifacts (design_report.md §9.2) -----------------------------------------------------------------

# The report is shown in a sandboxed iframe and printed by a script-free browser, so it never needs more.
ARTIFACT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-ancestors 'self'"
ARTIFACT_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}
_ARTIFACT_COLUMNS = "id, tier, html_path, pdf_path, pdf_status, generation_job_id"


def _get_artifact_row(auth: AuthContext, report_id: str) -> dict:
    row = _first(auth.client.table("assessment_reports").select(_ARTIFACT_COLUMNS).eq("org_id", auth.org_id).eq("id", report_id))
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row


def _read_artifact(auth: AuthContext, row: dict, column: str, expected: str) -> bytes:
    """The stored bytes, only from this report's own `{org_id}/{report_id}/` folder: the path comes from
    our own row, but is checked anyway so a corrupted row can never read another org's object."""
    path = row.get(column)
    if not path or path != expected:
        raise HTTPException(status_code=404, detail="this report has no stored file of that kind")
    try:
        return auth.client.storage.from_(artifacts.BUCKET).download(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read %s for report %s", column, row["id"], exc_info=True)
        raise HTTPException(status_code=404, detail="this report has no stored file of that kind") from exc


@router.get("/api/reports/{report_id}/html")
async def get_report_html(report_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    row = _get_artifact_row(auth, report_id)
    body = _read_artifact(auth, row, "html_path", artifacts.html_path(auth.org_id, report_id))
    return Response(
        content=body,
        media_type=artifacts.HTML_CONTENT_TYPE,
        headers={**ARTIFACT_HEADERS, "Content-Security-Policy": ARTIFACT_CSP},
    )


@router.get("/api/reports/{report_id}/pdf")
async def get_report_pdf(report_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    row = _get_artifact_row(auth, report_id)
    if row["pdf_status"] == "pending":
        raise HTTPException(status_code=409, detail="the PDF is not ready yet")
    if row["pdf_status"] == "failed":
        raise HTTPException(status_code=409, detail="the PDF could not be generated; retry it")
    body = _read_artifact(auth, row, "pdf_path", artifacts.pdf_path(auth.org_id, report_id))
    return Response(
        content=body,
        media_type=artifacts.PDF_CONTENT_TYPE,
        headers={
            **ARTIFACT_HEADERS,
            "Content-Disposition": f'attachment; filename="report-{row["tier"]}-{report_id[:8]}.pdf"',
        },
    )


@router.post("/api/reports/{report_id}/pdf/retry", status_code=202)
async def retry_report_pdf(report_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    row = _get_artifact_row(auth, report_id)
    if row["pdf_status"] != "failed":
        raise HTTPException(status_code=409, detail="only a failed PDF can be retried")
    if not row.get("html_path"):
        raise HTTPException(status_code=409, detail="this report has no stored HTML to render a PDF from")

    service_client = get_service_role_client()
    _set_pdf_status(service_client, auth.org_id, report_id, "pending")  # a second click is now a 409
    try:
        await enqueue_report_job(report_job_id=row["generation_job_id"], org_id=auth.org_id, stage="render_pdf")
    except Exception:
        logger.exception("could not enqueue a pdf retry for report %s", report_id)
        _set_pdf_status(service_client, auth.org_id, report_id, "failed")
        raise
    return {"status": "pending"}


def _set_pdf_status(service_client, org_id: str, report_id: str, status: str) -> None:
    service_client.table("assessment_reports").update({"pdf_status": status}).eq("id", report_id).eq("org_id", org_id).execute()


# --- operator-only internal estimate (design_report.md §9.5) ----------------------------------------------------


@router.get("/api/reports/{report_id}/internal-estimate")
async def get_internal_estimate(report_id: str, auth: Annotated[AuthContext, Depends(get_cost_operator)]):
    if _first(auth.client.table("assessment_reports").select("id").eq("org_id", auth.org_id).eq("id", report_id)) is None:
        raise HTTPException(status_code=404, detail="report not found")
    # No tenant privileges on this table (design_report.md §3): service role, filtered by org and report.
    row = _first(
        get_service_role_client()
        .table("report_internal_estimates")
        .select("report_id, ai_cost_p50, ai_cost_p90, n, calibration, price_basis, created_at")
        .eq("org_id", auth.org_id)
        .eq("report_id", report_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no internal estimate for this report")
    return {
        "report_id": row["report_id"],
        "ai_cost_p50": row.get("ai_cost_p50"),
        "ai_cost_p90": row.get("ai_cost_p90"),
        "n": row.get("n", 0),
        "calibration": row.get("calibration"),
        "price_basis": row.get("price_basis"),
        "created_at": row.get("created_at"),
    }


# --- share-link management (design_report.md §9.3) ------------------------------------------------------------

_SHARE_LIST_COLUMNS = "id, created_at, expires_at, revoked_at, access_count, last_accessed_at"


class CreateShareLink(BaseModel):
    expires_in_days: int | None = None


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _share_status(row: dict, now: datetime) -> str:
    if row.get("revoked_at"):
        return "revoked"
    expires_at = row["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    return "expired" if expires_at <= now else "active"


def _share_view(row: dict, now: datetime) -> dict:
    """Rebuilt explicitly so token_hash, created_by and org_id can't reach a response."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row.get("revoked_at"),
        "access_count": row.get("access_count", 0),
        "last_accessed_at": row.get("last_accessed_at"),
        "status": _share_status(row, now),
    }


@router.post("/api/reports/{report_id}/share-links", status_code=201)
async def create_share_link(report_id: str, body: CreateShareLink, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if _first(auth.client.table("assessment_reports").select("id").eq("org_id", auth.org_id).eq("id", report_id)) is None:
        raise HTTPException(status_code=404, detail="report not found")

    sharing = load_config()[0]["sharing"]
    days = body.expires_in_days if body.expires_in_days is not None else sharing["default_expiry_days"]
    if days < 1:
        raise HTTPException(status_code=422, detail="expires_in_days must be at least 1")
    days = min(days, sharing["max_expiry_days"])

    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    row = insert_scoped(
        get_service_role_client(),
        "report_share_links",
        auth.org_id,
        {
            "report_id": report_id,
            "token_hash": hash_share_token(token),
            "created_by": auth.user_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=days)).isoformat(),
        },
    ).data[0]
    return {
        "id": row["id"],
        "token": token,
        "share_path": f"/?share={token}",
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


@router.get("/api/reports/{report_id}/share-links")
async def list_share_links(report_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if _first(auth.client.table("assessment_reports").select("id").eq("org_id", auth.org_id).eq("id", report_id)) is None:
        raise HTTPException(status_code=404, detail="report not found")
    rows = (
        auth.client.table("report_share_links")
        .select(_SHARE_LIST_COLUMNS)
        .eq("org_id", auth.org_id)
        .eq("report_id", report_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    now = datetime.now(UTC)
    return {"share_links": [_share_view(row, now) for row in rows]}


@router.post("/api/share-links/{link_id}/revoke")
async def revoke_share_link(link_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    row = _first(
        auth.client.table("report_share_links").select("id, revoked_at").eq("org_id", auth.org_id).eq("id", link_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="share link not found")
    revoked_at = row.get("revoked_at")
    if not revoked_at:
        revoked_at = datetime.now(UTC).isoformat()
        (
            get_service_role_client()
            .table("report_share_links")
            .update({"revoked_at": revoked_at, "revoked_by": auth.user_id})
            .eq("id", link_id)
            .eq("org_id", auth.org_id)
            .execute()
        )
    return {"id": link_id, "revoked_at": revoked_at, "status": "revoked"}
