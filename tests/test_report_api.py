"""Assessment Report API (report/router.py) — design_report.md §9.1.

One FakeClient stands in for both the RLS-scoped and service-role clients, auth is overridden
(same setup as tests/test_agent3_executions_api.py)."""

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.report import router as report_router_module
from tests.fakes import FakeClient

ORG, OTHER = "org-123", "org-999"
PROJECT, JOB, NEW_JOB = "project-1", "job-1", "job-2"


@pytest.fixture
def fake_client():
    return FakeClient(org_id=ORG)


@pytest.fixture
def enqueued(monkeypatch):
    sent = []

    async def fake_enqueue(report_job_id, org_id, stage=None):
        sent.append((report_job_id, org_id) if stage is None else (report_job_id, org_id, stage))

    monkeypatch.setattr(report_router_module, "enqueue_report_job", fake_enqueue)
    return sent


@pytest.fixture
def client(fake_client, enqueued, monkeypatch):
    monkeypatch.setattr(report_router_module, "get_service_role_client", lambda: fake_client)
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


def _seed(fc, job_status="done", plans=(("plan-1", "approved", 1),), latest=JOB):
    fc.table("projects").insert({"id": PROJECT, "org_id": ORG, "source_ref": "x", "latest_job_id": latest}).execute()
    fc.table("ingestion_jobs").insert({"id": JOB, "org_id": ORG, "status": job_status}).execute()
    for plan_id, status, version in plans:
        fc.table("migration_plans").insert(
            {"id": plan_id, "org_id": ORG, "project_id": PROJECT, "source_job_id": JOB, "status": status, "version": version}
        ).execute()


def _report(fc, report_id="r-1", org_id=ORG, tier="xray", plan_id=None, source_job_id=JOB, created_at=1, **kw):
    fc.table("assessment_reports").insert(
        {
            "id": report_id, "org_id": org_id, "project_id": PROJECT, "generation_job_id": f"gj-{report_id}",
            "tier": tier, "source_job_id": source_job_id, "plan_id": plan_id,
            "plan_version": 1 if plan_id else None, "plan_status_at_generation": "approved" if plan_id else None,
            "report_json": {"tier": tier}, "schema_version": 1, "template_version": "1", "estimator_version": "1",
            "exec_summary_status": "verified", "html_path": "org/r/report.html", "pdf_path": None,
            "pdf_status": "pending", "created_by": "user-1", "created_at": created_at, **kw,
        }
    ).execute()


def _jobs(fc):
    return fc.table("report_generation_jobs").rows


# --- POST ---------------------------------------------------------------------------------


def test_create_xray_queues_a_pinned_job(client, fake_client, enqueued):
    _seed(fake_client)
    response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"})
    assert response.status_code == 202
    [job] = _jobs(fake_client)
    assert response.json() == {"report_job_id": job["id"], "status": "queued"}
    assert (job["org_id"], job["project_id"], job["tier"], job["source_job_id"]) == (ORG, PROJECT, "xray", JOB)
    assert job["plan_id"] is None and job["plan_version"] is None
    assert job["status"] == "queued" and job["created_by"] == "user-1"
    assert set(job["params"]) == {"model_deployment", "max_grounding_retries", "pdf_timeout_seconds", "pdf_memory_mb", "renderer_image"}
    assert enqueued == [(job["id"], ORG)]


def test_create_full_pins_the_approved_plan_over_a_newer_draft(client, fake_client):
    _seed(fake_client, plans=(("plan-a", "approved", 1), ("plan-b", "draft", 2)))
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"}).status_code == 202
    [job] = _jobs(fake_client)
    assert (job["plan_id"], job["plan_version"]) == ("plan-a", 1)


def test_create_full_takes_the_highest_version_when_none_is_approved(client, fake_client):
    _seed(fake_client, plans=(("plan-a", "draft", 1), ("plan-b", "in_review", 2)))
    client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"})
    assert _jobs(fake_client)[0]["plan_id"] == "plan-b"


@pytest.mark.parametrize("plans", [(), (("p", "stale", 1),), (("p", "rejected", 1),)])
def test_full_without_an_eligible_plan_is_409(client, fake_client, enqueued, plans):
    _seed(fake_client, plans=plans)
    response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"})
    assert response.status_code == 409 and "plan" in response.json()["detail"]
    assert _jobs(fake_client) == [] and enqueued == []
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 202


def test_full_with_a_plan_for_an_older_job_is_409(client, fake_client):
    _seed(fake_client, latest=NEW_JOB)
    fake_client.table("ingestion_jobs").insert({"id": NEW_JOB, "org_id": ORG, "status": "done"}).execute()
    response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"})
    assert response.status_code == 409 and "no migration plan for the latest ingestion" in response.json()["detail"]


@pytest.mark.parametrize("job_status", ["running", "failed", "queued"])
def test_no_done_ingestion_is_409_for_both_tiers(client, fake_client, job_status):
    _seed(fake_client, job_status=job_status)
    for tier in ("xray", "full"):
        response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": tier})
        assert response.status_code == 409 and "no completed ingestion" in response.json()["detail"]
    assert _jobs(fake_client) == []


def test_project_without_a_job_is_409(client, fake_client):
    _seed(fake_client, latest=None)
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 409


def test_same_tier_already_active_is_409_but_the_other_tier_is_not(client, fake_client):
    _seed(fake_client)
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 202
    again = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"})
    assert again.status_code == 409 and "already queued or running" in again.json()["detail"]
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"}).status_code == 202


def test_finished_job_does_not_block_a_new_one(client, fake_client):
    _seed(fake_client)
    client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"})
    _jobs(fake_client)[0]["status"] = "done"
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 202


def test_race_on_the_unique_index_is_409(client, fake_client, monkeypatch):
    _seed(fake_client)

    def racing(*_args, **_kwargs):
        raise APIError({"code": "23505", "message": "duplicate key value violates unique constraint"})

    monkeypatch.setattr(report_router_module, "insert_scoped", racing)
    response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"})
    assert response.status_code == 409 and "already queued or running" in response.json()["detail"]


def test_enqueue_failure_marks_the_job_failed_so_it_cannot_block(client, fake_client, monkeypatch):
    _seed(fake_client)

    async def broken(report_job_id, org_id):
        raise ConnectionError("queue down")

    monkeypatch.setattr(report_router_module, "enqueue_report_job", broken)
    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 500
    [job] = _jobs(fake_client)
    assert job["status"] == "failed" and "enqueueing" in job["error"]


@pytest.mark.parametrize("body", [{"tier": "gold"}, {}, {"tier": None}])
def test_bad_tier_is_422(client, fake_client, body):
    _seed(fake_client)
    assert client.post(f"/api/projects/{PROJECT}/reports", json=body).status_code == 422


def test_other_orgs_project_is_404(client, fake_client):
    fake_client.table("projects").insert({"id": "p-x", "org_id": OTHER, "source_ref": "x", "latest_job_id": JOB}).execute()
    assert client.post("/api/projects/p-x/reports", json={"tier": "xray"}).status_code == 404
    assert client.get("/api/projects/p-x/reports").status_code == 404
    assert _jobs(fake_client) == []


# --- list ----------------------------------------------------------------------------------


def test_list_is_newest_first_without_report_json_or_paths(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-old", created_at=1)
    _report(fake_client, "r-new", tier="full", plan_id="plan-1", created_at=2)
    body = client.get(f"/api/projects/{PROJECT}/reports").json()
    assert [r["id"] for r in body["reports"]] == ["r-new", "r-old"]
    first = body["reports"][0]
    assert (first["tier"], first["plan_version"], first["plan_status"], first["out_of_date"]) == ("full", 1, "approved", False)
    assert first["exec_summary_status"] == "verified" and first["pdf_status"] == "pending"
    assert not {"report_json", "html_path", "pdf_path", "org_id", "created_by"} & set(first)


def test_list_out_of_date_when_the_project_moved_to_a_newer_job(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1")
    fake_client.table("projects").rows[0]["latest_job_id"] = NEW_JOB
    assert client.get(f"/api/projects/{PROJECT}/reports").json()["reports"][0]["out_of_date"] is True


@pytest.mark.parametrize("status,expected", [("stale", True), ("rejected", True), ("in_review", False), ("approved", False)])
def test_list_out_of_date_follows_the_plans_current_status(client, fake_client, status, expected):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    fake_client.table("migration_plans").rows[0]["status"] = status
    item = client.get(f"/api/projects/{PROJECT}/reports").json()["reports"][0]
    assert item["out_of_date"] is expected and item["plan_status"] == status
    assert item["plan_status_at_generation"] == "approved"


def test_list_never_writes_to_agent_tables(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    fake_client.table("projects").rows[0]["latest_job_id"] = NEW_JOB  # a stale plan on read
    client.get(f"/api/projects/{PROJECT}/reports")
    assert fake_client.table("migration_plans").rows[0]["status"] == "approved"  # no mark_stale_plans


def test_list_hides_other_orgs_reports_and_jobs(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-mine")
    _report(fake_client, "r-theirs", org_id=OTHER)
    fake_client.table("report_generation_jobs").insert(
        {"id": "j-theirs", "org_id": OTHER, "project_id": PROJECT, "tier": "xray", "status": "running"}
    ).execute()
    body = client.get(f"/api/projects/{PROJECT}/reports").json()
    assert [r["id"] for r in body["reports"]] == ["r-mine"] and body["active_jobs"] == []


def test_list_reports_active_jobs_and_eligibility(client, fake_client):
    _seed(fake_client, plans=())
    client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"})
    body = client.get(f"/api/projects/{PROJECT}/reports").json()
    [active] = body["active_jobs"]
    assert (active["tier"], active["status"]) == ("xray", "queued") and set(active) == {"id", "tier", "status", "progress_stage", "created_at"}
    assert body["eligibility"]["xray"]["eligible"] is False and "already queued" in body["eligibility"]["xray"]["reason"]
    assert body["eligibility"]["full"] == {"eligible": False, "reason": "no migration plan for the latest ingestion yet"}


def test_list_eligibility_when_everything_is_ready(client, fake_client):
    _seed(fake_client)
    eligibility = client.get(f"/api/projects/{PROJECT}/reports").json()["eligibility"]
    assert eligibility == {"xray": {"eligible": True, "reason": None}, "full": {"eligible": True, "reason": None}}


# --- detail ----------------------------------------------------------------------------------


def test_get_report_returns_the_report_without_paths(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    body = client.get("/api/reports/r-1").json()
    assert body["report_json"] == {"tier": "full"} and body["out_of_date"] is False
    assert (body["plan_status"], body["has_html"], body["pdf_status"]) == ("approved", True, "pending")
    assert not {"html_path", "pdf_path", "org_id", "created_by"} & set(body)


def test_get_report_out_of_date(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", source_job_id="job-old")
    assert client.get("/api/reports/r-1").json()["out_of_date"] is True


def test_get_report_of_another_org_is_404(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-theirs", org_id=OTHER)
    assert client.get("/api/reports/r-theirs").status_code == 404
    assert client.get("/api/reports/missing").status_code == 404


# --- events ------------------------------------------------------------------------------------


def _event(fc, kind="report_generation", workflow_id="j-1", org_id=ORG, event_type="activity", stage="collect"):
    fc.table("workflow_events").insert(
        {"org_id": org_id, "workflow_kind": kind, "workflow_id": workflow_id, "event_type": event_type, "stage": stage, "message": "m", "data": {}}
    ).execute()


def _job(fc, job_id="j-1", org_id=ORG, status="running"):
    fc.table("report_generation_jobs").insert(
        {"id": job_id, "org_id": org_id, "project_id": PROJECT, "tier": "xray", "status": status, "progress_stage": "collect", "error": None}
    ).execute()


def test_events_filter_by_kind_job_and_cursor(client, fake_client):
    _job(fake_client)
    for kind, wid in [("report_generation", "j-1"), ("report_generation", "j-1"), ("agent1_ingestion", "j-1"), ("report_generation", "j-2")]:
        _event(fake_client, kind=kind, workflow_id=wid)
    body = client.get("/api/reports/jobs/j-1/events").json()
    assert len(body["events"]) == 2 and body["status"] == "running" and body["progress_stage"] == "collect"
    assert body["report_id"] is None and body["tier"] == "xray" and body["error"] is None
    assert all("org_id" not in e for e in body["events"])
    after = client.get(f"/api/reports/jobs/j-1/events?after={body['next_cursor']}").json()
    assert after["events"] == [] and after["next_cursor"] == body["next_cursor"]


def test_events_carry_the_report_id_once_published(client, fake_client):
    _job(fake_client, status="done")
    _report(fake_client, "r-1", generation_job_id="j-1")
    assert client.get("/api/reports/jobs/j-1/events").json()["report_id"] == "r-1"


def test_events_of_another_orgs_job_are_404(client, fake_client):
    _job(fake_client, job_id="j-x", org_id=OTHER)
    _event(fake_client, workflow_id="j-x", org_id=OTHER)
    assert client.get("/api/reports/jobs/j-x/events").status_code == 404


def test_events_limit_is_validated(client, fake_client):
    _job(fake_client)
    assert client.get("/api/reports/jobs/j-1/events?limit=0").status_code == 422
    assert client.get("/api/reports/jobs/j-1/events?after=-1").status_code == 422


# --- stored artifacts: HTML, PDF and the PDF retry (design_report.md section 9.2) -----------------------------------

HTML = "<!doctype html><title>Report</title><p>hello</p>"
PDF = b"%PDF-1.7 fake pdf bytes"


def _artifact_report(fc, report_id="r-1", org_id=ORG, pdf_status="done", html=True, pdf=True, tier="xray"):
    """A report row whose files really are in the (fake) `reports` bucket, under `{org}/{id}/`."""
    html_path = f"{org_id}/{report_id}/report.html" if html else None
    pdf_path = f"{org_id}/{report_id}/report.pdf" if pdf else None
    bucket = fc.storage.from_("reports")
    if html:
        bucket.upload(html_path, HTML.encode("utf-8"))
    if pdf:
        bucket.upload(pdf_path, PDF)
    _report(fc, report_id, org_id=org_id, tier=tier, html_path=html_path, pdf_path=pdf_path, pdf_status=pdf_status)


def _row(fc, report_id="r-1"):
    return next(r for r in fc.table("assessment_reports").rows if r["id"] == report_id)


def test_html_is_served_with_the_strict_csp_and_no_store(client, fake_client):
    _artifact_report(fake_client)
    response = client.get("/api/reports/r-1/html")
    assert response.status_code == 200 and response.text == HTML
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-ancestors 'self'"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer" and response.headers["x-robots-tag"] == "noindex"


def test_html_404s(client, fake_client):
    _artifact_report(fake_client, "r-nohtml", html=False)
    _artifact_report(fake_client, "r-theirs", org_id=OTHER)
    assert client.get("/api/reports/r-nohtml/html").status_code == 404  # no stored HTML
    assert client.get("/api/reports/r-theirs/html").status_code == 404  # another org's report
    assert client.get("/api/reports/missing/html").status_code == 404
    _artifact_report(fake_client, "r-gone")
    del fake_client.storage.from_("reports")._objects[f"{ORG}/r-gone/report.html"]
    assert client.get("/api/reports/r-gone/html").status_code == 404  # row says yes, bucket says no


def test_a_row_pointing_at_another_orgs_object_is_never_read(client, fake_client):
    _artifact_report(fake_client, "r-theirs", org_id=OTHER)
    _artifact_report(fake_client)
    _row(fake_client)["html_path"] = f"{OTHER}/r-theirs/report.html"  # a corrupted row
    _row(fake_client)["pdf_path"] = f"{OTHER}/r-theirs/report.pdf"
    assert client.get("/api/reports/r-1/html").status_code == 404
    assert client.get("/api/reports/r-1/pdf").status_code == 404


def test_pdf_is_streamed_as_an_attachment(client, fake_client):
    _artifact_report(fake_client, tier="full")
    response = client.get("/api/reports/r-1/pdf")
    assert response.status_code == 200 and response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="report-full-r-1.pdf"'
    assert response.headers["cache-control"] == "no-store" and response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers  # that is for the HTML


def test_pdf_not_ready_and_failed_are_409s_with_a_reason(client, fake_client):
    _artifact_report(fake_client, "r-pending", pdf_status="pending", pdf=False)
    _artifact_report(fake_client, "r-failed", pdf_status="failed", pdf=False)
    pending, failed = client.get("/api/reports/r-pending/pdf"), client.get("/api/reports/r-failed/pdf")
    assert pending.status_code == 409 and "not ready" in pending.json()["detail"]
    assert failed.status_code == 409 and "retry" in failed.json()["detail"]


def test_pdf_404s(client, fake_client):
    _artifact_report(fake_client, "r-theirs", org_id=OTHER)
    _artifact_report(fake_client, "r-nofile")
    del fake_client.storage.from_("reports")._objects[f"{ORG}/r-nofile/report.pdf"]
    assert client.get("/api/reports/r-theirs/pdf").status_code == 404
    assert client.get("/api/reports/r-nofile/pdf").status_code == 404
    assert client.get("/api/reports/missing/pdf").status_code == 404


def test_retry_flips_to_pending_and_queues_a_pdf_only_message(client, fake_client, enqueued):
    _artifact_report(fake_client, pdf_status="failed", pdf=False)
    response = client.post("/api/reports/r-1/pdf/retry")
    assert response.status_code == 202 and response.json() == {"status": "pending"}
    assert _row(fake_client)["pdf_status"] == "pending"
    assert enqueued == [("gj-r-1", ORG, "render_pdf")]  # the report's own generation job, stage render_pdf


def test_a_second_retry_is_a_409_and_queues_nothing(client, fake_client, enqueued):
    _artifact_report(fake_client, pdf_status="failed", pdf=False)
    assert client.post("/api/reports/r-1/pdf/retry").status_code == 202
    again = client.post("/api/reports/r-1/pdf/retry")
    assert again.status_code == 409 and "failed PDF" in again.json()["detail"]
    assert len(enqueued) == 1


@pytest.mark.parametrize("status", ["pending", "done"])
def test_only_a_failed_pdf_can_be_retried(client, fake_client, enqueued, status):
    _artifact_report(fake_client, pdf_status=status)
    assert client.post("/api/reports/r-1/pdf/retry").status_code == 409
    assert enqueued == [] and _row(fake_client)["pdf_status"] == status


def test_retry_needs_stored_html(client, fake_client, enqueued):
    _artifact_report(fake_client, pdf_status="failed", html=False, pdf=False)
    response = client.post("/api/reports/r-1/pdf/retry")
    assert response.status_code == 409 and "HTML" in response.json()["detail"]
    assert enqueued == [] and _row(fake_client)["pdf_status"] == "failed"


def test_retry_enqueue_failure_restores_failed_so_it_can_be_tried_again(client, fake_client, monkeypatch):
    _artifact_report(fake_client, pdf_status="failed", pdf=False)

    async def broken(report_job_id, org_id, stage=None):
        raise ConnectionError("queue down")

    monkeypatch.setattr(report_router_module, "enqueue_report_job", broken)
    assert client.post("/api/reports/r-1/pdf/retry").status_code == 500
    assert _row(fake_client)["pdf_status"] == "failed"


def test_retry_of_another_orgs_report_is_404_and_changes_nothing(client, fake_client, enqueued):
    _artifact_report(fake_client, "r-theirs", org_id=OTHER, pdf_status="failed", pdf=False)
    assert client.post("/api/reports/r-theirs/pdf/retry").status_code == 404
    assert enqueued == [] and _row(fake_client, "r-theirs")["pdf_status"] == "failed"


def test_artifact_responses_never_carry_storage_paths_or_org_ids(client, fake_client):
    _artifact_report(fake_client)
    for path in ("/api/reports/r-1/html", "/api/reports/r-1/pdf"):
        response = client.get(path)
        assert ORG not in str(dict(response.headers)) and "report.html" not in str(dict(response.headers))
