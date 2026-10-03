"""Report artifacts in the private `reports` bucket (design_report.md §9.2).

Objects live at `{org_id}/{report_id}/report.html` and `report.pdf`: deterministic, so a
redelivered job overwrites instead of piling up, and the first path segment is the org the
bucket's select policy scopes reads by. Only the service-role worker writes.

`render_and_store_pdf` is the one PDF flow, shared by the `render_pdf` stage and the PDF-only
retry: render the HTML in the hardened container, extract the PDF's text, run the rendered gate
over it, and upload only if clean. Nothing here raises for a PDF problem: the PDF can fail without
failing the report, so every outcome is a `PdfResult`.
"""

import io
import logging
from dataclasses import dataclass

from supabase import Client

from document_analyzer.report import leak_gate
from document_analyzer.report import pdf as pdf_module
from document_analyzer.report.pdf import PdfParams

logger = logging.getLogger(__name__)

BUCKET = "reports"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
PDF_CONTENT_TYPE = "application/pdf"


def html_path(org_id: str, report_id: str) -> str:
    return f"{org_id}/{report_id}/report.html"


def pdf_path(org_id: str, report_id: str) -> str:
    return f"{org_id}/{report_id}/report.pdf"


def upload(client: Client, path: str, data: bytes, content_type: str) -> None:
    client.storage.from_(BUCKET).upload(path, data, {"content-type": content_type, "upsert": "true"})


def download(client: Client, path: str) -> bytes:
    return client.storage.from_(BUCKET).download(path)


def pdf_text(pdf: bytes) -> str:
    """The text of every page, for the rendered gate."""
    from pypdf import PdfReader

    return " ".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(pdf)).pages)


@dataclass(frozen=True)
class PdfResult:
    status: str  # "done" or "failed"
    path: str | None = None
    reason: str | None = None  # why it failed, for events and logs: never report content


def render_and_store_pdf(
    client: Client,
    *,
    org_id: str,
    report_id: str,
    html: str,
    report_date: str,
    params: PdfParams,
    index: leak_gate.SourceIndex | None,
) -> PdfResult:
    try:
        pdf = pdf_module.render_pdf(html, report_id, report_date, params)
        reasons = leak_gate.scan_rendered(pdf_text(pdf), index)
        if reasons:
            # The artifact itself is the problem: nothing is uploaded (design_report.md §8.1 gate 3).
            return PdfResult("failed", None, f"the PDF text failed the content gate: {', '.join(reasons)}")
        path = pdf_path(org_id, report_id)
        upload(client, path, pdf, PDF_CONTENT_TYPE)
        return PdfResult("done", path)
    except Exception as exc:  # noqa: BLE001 — a PDF problem never fails a report
        logger.warning("pdf for report %s failed: %s", report_id, type(exc).__name__, exc_info=True)
        return PdfResult("failed", None, f"{type(exc).__name__}: {str(exc)[:300]}")
