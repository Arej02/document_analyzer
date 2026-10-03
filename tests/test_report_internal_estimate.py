"""GET /api/reports/{report_id}/internal-estimate (report/router.py) — design_report.md §9.5.

Operator-only: `get_cost_operator` answers 404 to everyone else, before anything is read."""

import pytest
from fastapi.testclient import TestClient

from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.report import router as report_router_module
from document_analyzer.shared.cost_access import COST_OPERATOR_ENV
from tests.fakes import FakeClient

ORG, OTHER = "org-123", "org-999"
PATH = "/api/reports/r-1/internal-estimate"
CALIBRATION = {
    "snapshot": {"id": "snap-1"},
    "buckets": [{"size": "small", "risk": "low", "units": 3, "n": 25, "source": "historical", "median_cost_usd": 0.42}],
    "unpriced_excluded": 2,
    "unpriced_share": 0.07,
    "low_confidence": False,
}
PRICE_BASIS = {"method": "markup", "currency": "USD", "pricing_version": "1"}


@pytest.fixture
def fake_client():
    return FakeClient(org_id=ORG)


def _client(fake_client, user_id, org_id=ORG):
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id=user_id, org_id=org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def operator(fake_client, monkeypatch):
    monkeypatch.setenv(COST_OPERATOR_ENV, "operator-1")
    monkeypatch.setattr(report_router_module, "get_service_role_client", lambda: fake_client)
    fake_client.table("assessment_reports").insert({"id": "r-1", "org_id": ORG, "project_id": "p-1", "tier": "full"}).execute()
    fake_client.table("report_internal_estimates").insert(
        {
            "org_id": ORG, "report_id": "r-1", "ai_cost_p50": 12.5, "ai_cost_p90": 20.25, "n": 25,
            "calibration": CALIBRATION, "price_basis": PRICE_BASIS, "created_at": "2026-10-01T12:00:00+00:00",
        }
    ).execute()
    return _client(fake_client, "operator-1")


def test_an_operator_gets_the_estimate_rebuilt_without_org_id(operator):
    response = operator.get(PATH)

    assert response.status_code == 200
    assert response.json() == {
        "report_id": "r-1", "ai_cost_p50": 12.5, "ai_cost_p90": 20.25, "n": 25,
        "calibration": CALIBRATION, "price_basis": PRICE_BASIS, "created_at": "2026-10-01T12:00:00+00:00",
    }


def test_a_cold_start_estimate_keeps_its_nulls(operator, fake_client):
    row = fake_client.table("report_internal_estimates").rows[0]
    row.update(ai_cost_p50=None, ai_cost_p90=None, n=0, price_basis=None)

    body = operator.get(PATH).json()

    assert (body["ai_cost_p50"], body["ai_cost_p90"], body["n"], body["price_basis"]) == (None, None, 0, None)


@pytest.mark.parametrize("user_id", ["member-1", ""])
def test_a_non_operator_gets_the_plain_404_and_nothing_is_read(operator, fake_client, monkeypatch, user_id):
    touched = []
    original = fake_client.table
    monkeypatch.setattr(fake_client, "table", lambda name: (touched.append(name), original(name))[1])

    response = _client(fake_client, user_id).get(PATH)

    assert (response.status_code, response.json()) == (404, {"detail": "not found"})
    assert touched == []


def test_nobody_is_an_operator_by_default(operator, fake_client, monkeypatch):
    monkeypatch.delenv(COST_OPERATOR_ENV)

    assert _client(fake_client, "operator-1").get(PATH).status_code == 404


def test_an_operator_of_another_org_cannot_read_it(operator, fake_client):
    response = _client(fake_client, "operator-1", org_id=OTHER).get(PATH)

    assert (response.status_code, response.json()) == (404, {"detail": "report not found"})


def test_a_missing_report_or_a_report_without_an_estimate_is_404(operator, fake_client):
    assert operator.get("/api/reports/nope/internal-estimate").status_code == 404
    fake_client.table("assessment_reports").insert({"id": "xray", "org_id": ORG, "project_id": "p-1", "tier": "xray"}).execute()

    response = operator.get("/api/reports/xray/internal-estimate")

    assert (response.status_code, response.json()) == (404, {"detail": "no internal estimate for this report"})


def test_an_estimate_row_of_another_org_is_never_returned(operator, fake_client):
    fake_client.table("report_internal_estimates").rows[0]["org_id"] = OTHER

    assert operator.get(PATH).status_code == 404


def test_missing_auth_is_401():
    assert TestClient(create_app()).get(PATH).status_code == 401


def test_no_cost_figure_reaches_the_customer_endpoints(operator, fake_client):
    """The detail and list endpoints never carry the internal estimate, even for an operator."""
    fake_client.table("assessment_reports").rows[0].update(
        project_id="p-1", generation_job_id="g", source_job_id="j", plan_id=None, plan_version=None,
        plan_status_at_generation=None, report_json={"tier": "full"}, schema_version=1, template_version="1",
        estimator_version="1", exec_summary_status="verified", pdf_status="pending", html_path=None, created_at=1,
    )
    fake_client.table("projects").insert({"id": "p-1", "org_id": ORG, "latest_job_id": None}).execute()

    detail = operator.get("/api/reports/r-1")
    listing = operator.get("/api/projects/p-1/reports")

    assert detail.status_code == listing.status_code == 200
    for text in (detail.text, listing.text):
        for needle in ("ai_cost", "12.5", "20.25", "calibration", "price_basis", "median_cost_usd"):
            assert needle not in text, needle
