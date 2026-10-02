"""Public share-link routes — design_report.md §9.4, §11.

No auth: the token is the credential. Every failure (malformed, unknown, expired, revoked, deleted
report or project, missing file) is the same 404. The routes read `report_share_links` and the
report's own `assessment_reports` row only, never `projects` or `migration_plans`, so nothing about
staleness can leak, and they accept no parameter besides the token.
"""

import hmac
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response

from ..supabase_client import get_service_role_client
from .router import ARTIFACT_CSP, ARTIFACT_HEADERS, hash_share_token
from .worker import artifacts

logger = logging.getLogger(__name__)

router = APIRouter(tags=["report-public"])

# secrets.token_urlsafe(32): 32 bytes, unpadded URL-safe base64.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_WINDOW_SECONDS = 60.0
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_limit() -> int:
    return int(os.environ.get("REPORT_PUBLIC_RATE_LIMIT", 30))


def _check_rate_limit(client_ip: str) -> None:
    limit = _rate_limit()
    if limit <= 0:
        return
    now = time.monotonic()
    with _hits_lock:
        window = _hits[client_ip]
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(_WINDOW_SECONDS - (now - window[0])) + 1)
            raise HTTPException(status_code=429, detail="too many requests", headers={**ARTIFACT_HEADERS, "Retry-After": str(retry_after)})
        window.append(now)


def reset_rate_limits() -> None:
    with _hits_lock:
        _hits.clear()


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found", headers=ARTIFACT_HEADERS)


def _first(query) -> dict | None:
    rows = query.limit(1).execute().data
    return rows[0] if rows else None


def _resolve(client, token: str) -> tuple[dict, dict]:
    """`(link, report)` for a usable token, else the one 404."""
    if not _TOKEN_RE.fullmatch(token):
        raise _not_found()
    token_hash = hash_share_token(token)
    link = _first(
        client.table("report_share_links")
        .select("id, org_id, report_id, token_hash, expires_at, revoked_at")
        .eq("token_hash", token_hash)
    )
    if link is None or not hmac.compare_digest(link["token_hash"], token_hash) or link.get("revoked_at"):
        raise _not_found()
    expires_at = link["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at <= datetime.now(UTC):
        raise _not_found()
    report = _first(
        client.table("assessment_reports")
        .select("id, created_at, html_path, pdf_path")
        .eq("org_id", link["org_id"])
        .eq("id", link["report_id"])
    )
    if report is None:
        raise _not_found()
    return link, report


def _record_access(client, link: dict) -> None:
    """Best effort: an audit counter must never fail a view."""
    try:
        row = _first(client.table("report_share_links").select("access_count").eq("id", link["id"]))
        (
            client.table("report_share_links")
            .update({"access_count": (row or {}).get("access_count", 0) + 1, "last_accessed_at": datetime.now(UTC).isoformat()})
            .eq("id", link["id"])
            .eq("org_id", link["org_id"])
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not record access for share link %s", link["id"], exc_info=True)


def _read(client, link: dict, report: dict, column: str, expected: str) -> bytes:
    if not report.get(column) or report[column] != expected:
        raise _not_found()
    try:
        return artifacts.download(client, expected)
    except Exception:  # noqa: BLE001
        logger.warning("could not read %s for shared report %s", column, report["id"], exc_info=True)
        raise _not_found() from None


@router.get("/api/public/reports/{token}")
async def get_public_report(token: str, request: Request):
    _check_rate_limit(request.client.host if request.client else "unknown")
    client = get_service_role_client()
    link, report = _resolve(client, token)
    body = _read(client, link, report, "html_path", artifacts.html_path(link["org_id"], link["report_id"]))
    _record_access(client, link)
    return Response(
        content=body,
        media_type=artifacts.HTML_CONTENT_TYPE,
        headers={
            **ARTIFACT_HEADERS,
            "Content-Security-Policy": ARTIFACT_CSP,
            "X-Report-Generated-At": str(report["created_at"]),
            "X-Report-Pdf": "available" if report.get("pdf_path") else "unavailable",
        },
    )


@router.get("/api/public/reports/{token}/pdf")
async def get_public_report_pdf(token: str, request: Request):
    _check_rate_limit(request.client.host if request.client else "unknown")
    client = get_service_role_client()
    link, report = _resolve(client, token)
    body = _read(client, link, report, "pdf_path", artifacts.pdf_path(link["org_id"], link["report_id"]))
    _record_access(client, link)
    return Response(
        content=body,
        media_type=artifacts.PDF_CONTENT_TYPE,
        headers={
            **ARTIFACT_HEADERS,
            "Content-Disposition": f'attachment; filename="report-{str(link["report_id"])[:8]}.pdf"',
        },
    )
