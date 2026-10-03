"""Out of date is computed on read and never written — design_report.md §10.

A report is pinned to one ingestion job and, for `full`, one plan. It is out of date when the
project's latest job moved on, or when its plan is now `stale` or `rejected`. Reading it must not
write a row in any Agent 1 or Agent 2 table (in particular, `mark_stale_plans` is never called)."""

import copy

import pytest

# The fixtures and seeding helpers are shared with the API tests.
from tests.test_report_api import (  # noqa: F401  (fixtures)
    JOB,
    NEW_JOB,
    PROJECT,
    _report,
    _seed,
    client,
    enqueued,
    fake_client,
)

AGENT_TABLES = ("projects", "ingestion_jobs", "migration_plans", "planning_jobs", "clusters", "extracted_entities")


def _out_of_date(client, report_id="r-1"):
    detail = client.get(f"/api/reports/{report_id}").json()["out_of_date"]
    listed = {r["id"]: r["out_of_date"] for r in client.get(f"/api/projects/{PROJECT}/reports").json()["reports"]}
    assert listed[report_id] is detail  # the list and the detail never disagree
    return detail


def _reingest(fake_client):
    """A newer ingestion run of the same project, which becomes its latest."""
    fake_client.table("ingestion_jobs").insert({"id": NEW_JOB, "org_id": "org-123", "status": "done"}).execute()
    fake_client.table("projects").rows[0]["latest_job_id"] = NEW_JOB


def test_a_current_report_is_not_out_of_date(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1")

    assert _out_of_date(client) is False


@pytest.mark.parametrize("tier,plan_id", [("xray", None), ("full", "plan-1")])
def test_reingesting_makes_older_reports_out_of_date(client, fake_client, tier, plan_id):
    _seed(fake_client)
    _report(fake_client, "r-1", tier=tier, plan_id=plan_id)
    assert _out_of_date(client) is False

    _reingest(fake_client)

    assert _out_of_date(client) is True


def test_only_the_reports_of_the_old_run_go_out_of_date(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "old", source_job_id=JOB)
    _reingest(fake_client)
    _report(fake_client, "new", source_job_id=NEW_JOB, created_at=2)

    assert (_out_of_date(client, "old"), _out_of_date(client, "new")) == (True, False)


@pytest.mark.parametrize("status", ["stale", "rejected"])
def test_a_dead_plan_makes_its_full_assessment_out_of_date_but_not_an_xray(client, fake_client, status):
    _seed(fake_client)
    _report(fake_client, "full", tier="full", plan_id="plan-1")
    _report(fake_client, "xray", tier="xray", created_at=2)

    fake_client.table("migration_plans").rows[0]["status"] = status

    assert _out_of_date(client, "full") is True
    assert _out_of_date(client, "xray") is False


@pytest.mark.parametrize("status", ["draft", "in_review", "approved"])
def test_a_live_plan_does_not_make_a_report_out_of_date(client, fake_client, status):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")

    fake_client.table("migration_plans").rows[0]["status"] = status

    assert _out_of_date(client) is False


def test_the_plan_status_at_generation_is_kept_beside_the_current_one(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    fake_client.table("migration_plans").rows[0]["status"] = "stale"

    body = client.get("/api/reports/r-1").json()

    assert (body["plan_status_at_generation"], body["plan_status"]) == ("approved", "stale")


def test_out_of_date_is_never_stored(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    _reingest(fake_client)
    row = fake_client.table("assessment_reports").rows[0]
    before = copy.deepcopy(row)

    assert _out_of_date(client) is True

    assert row == before and "out_of_date" not in row


def test_reading_reports_writes_no_agent_1_or_2_row(client, fake_client):
    _seed(fake_client)
    _report(fake_client, "r-1", tier="full", plan_id="plan-1")
    _report(fake_client, "r-2", tier="xray", created_at=2)
    _reingest(fake_client)  # the plan's job is no longer the latest: the §9.1 stale check would move it
    fake_client.table("migration_plans").rows[0]["status"] = "approved"
    before = {t: copy.deepcopy(fake_client.table(t).rows) for t in AGENT_TABLES}

    for path in (
        f"/api/projects/{PROJECT}/reports",
        "/api/reports/r-1",
        "/api/reports/r-2",
        "/api/reports/r-1/html",
        "/api/reports/r-1/share-links",
    ):
        client.get(path)

    assert {t: fake_client.table(t).rows for t in AGENT_TABLES} == before
    assert fake_client.table("migration_plans").rows[0]["status"] == "approved"  # still approved: not marked stale


def test_a_shared_report_never_reveals_that_it_is_out_of_date(client, fake_client, monkeypatch):
    from document_analyzer.report import public as public_module

    monkeypatch.setattr(public_module, "get_service_role_client", lambda: fake_client)
    public_module.reset_rate_limits()
    _seed(fake_client)
    _report(fake_client, "r-1", html_path="org-123/r-1/report.html")
    fake_client.storage.from_("reports").upload("org-123/r-1/report.html", b"<html>snapshot</html>")
    token = client.post("/api/reports/r-1/share-links", json={}).json()["token"]
    _reingest(fake_client)
    assert _out_of_date(client) is True

    from fastapi.testclient import TestClient

    from document_analyzer.app import create_app

    public = TestClient(create_app(), raise_server_exceptions=False)
    response = public.get(f"/api/public/reports/{token}")

    assert response.status_code == 200 and response.content == b"<html>snapshot</html>"
    assert sorted(k for k in response.headers if k.startswith("x-report")) == ["x-report-generated-at", "x-report-pdf"]
