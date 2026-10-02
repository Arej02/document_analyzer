"""Reporter worker: report_generation_jobs status transitions — design_report.md §4.

Counterpart of agent3/worker/status.py against `report_generation_jobs`. Service-role
client; every update filters on both `id` and `org_id` (CLAUDE.md multi-tenancy rule), so a
desync between "which job" and "which org" is a no-op, never a write to another org's row.
"""

from supabase import Client

STAGES = ("collect", "estimate", "summarize", "render_html", "render_pdf", "publish")
FIRST_STAGE = STAGES[0]


def _update(client: Client, job_id: str, org_id: str, values: dict) -> None:
    client.table("report_generation_jobs").update(values).eq("id", job_id).eq("org_id", org_id).execute()


def mark_report_running(client: Client, job_id: str, org_id: str) -> None:
    _update(client, job_id, org_id, {"status": "running", "progress_stage": FIRST_STAGE})


def mark_report_stage(client: Client, job_id: str, org_id: str, progress_stage: str) -> None:
    _update(client, job_id, org_id, {"progress_stage": progress_stage})


def mark_report_failed(client: Client, job_id: str, org_id: str, error: str) -> None:
    """Job-level failure only; `progress_stage` stays where the job died. A PDF failure is
    `assessment_reports.pdf_status`, never this."""
    _update(client, job_id, org_id, {"status": "failed", "error": error})


def mark_report_done(client: Client, job_id: str, org_id: str) -> None:
    _update(client, job_id, org_id, {"status": "done", "progress_stage": None})


def is_cancelled(client: Client, job_id: str, org_id: str) -> bool:
    """Whether the job's status is `cancelled` (set outside the worker). A failed read is
    treated as not cancelled: a database blip must not kill a healthy job."""
    try:
        rows = (
            client.table("report_generation_jobs")
            .select("status")
            .eq("id", job_id)
            .eq("org_id", org_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(rows) and rows[0].get("status") == "cancelled"
