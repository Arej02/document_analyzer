"""Operator-only cost endpoints (design.md section 7.4): who may call them, what they return,
and that they never leak another org's numbers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_analyzer import jobs as jobs_module
from document_analyzer.agent2 import router as agent2_router
from document_analyzer.agent3 import router as agent3_router
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.shared import cost_access
from document_analyzer.shared.cost_access import COST_OPERATOR_ENV, is_cost_operator, operator_user_ids

from fakes import FakeClient

ORG, OTHER_ORG = "org-1", "org-2"
ENDPOINTS = {
    "ingestion": "/api/agent1/jobs/job-1/cost",
    "qa": "/api/agent1/jobs/job-1/qa-cost",
    "planning": "/api/agent2/planning-jobs/pj-1/cost",
    "execution": "/api/agent3/executions/ex-1/cost",
}


def _stats(calls=5, errors=0, unpriced=0, tokens=900, cost="0.012500"):
    return {"calls": calls, "error_calls": errors, "unpriced_calls": unpriced, "tokens": tokens, "cost_usd": cost}


def _seed(db):
    """The job tables the caller's own client reads, and the cost views/tables the service role reads."""
    for table, job in (("ingestion_jobs", "job-1"), ("planning_jobs", "pj-1"), ("execution_jobs", "ex-1")):
        db.table(table).insert({"id": job, "org_id": ORG, "summary_cache_hits": 3, "summary_cache_misses": 2}).execute()

    def view(name, **row):
        db.table(name).insert(row).execute()

    for org in (ORG, OTHER_ORG):
        mult = 1 if org == ORG else 100  # the other org's numbers are loud, so a leak is unmistakable
        for jt, job in (("ingestion", "job-1"), ("planning", "pj-1"), ("execution", "ex-1")):
            key = {"org_id": org, "job_type": jt, "job_id": job}
            view("llm_cost_by_job", **key, **_stats(calls=5 * mult))
            view("llm_cost_by_job_stage", **key, stage="summarize", **_stats(calls=3 * mult))
            view("llm_cost_by_job_stage", **key, stage="fold", **_stats(calls=2 * mult, cost="0.0005"))
            view("llm_cost_by_job_purpose", **key, purpose="entity_summary", **_stats(calls=5 * mult))
        view("llm_cost_by_execution_unit", org_id=org, job_id="ex-1", cluster_id="u1", rounds=2, max_attempt=3, **_stats())
        db.table("llm_invocations").insert(
            {"org_id": org, "job_type": "qa", "source_job_id": "job-1", "purpose": "qa_answer", "stage": None,
             "status": "ok", "cost_usd": "0.001", "total_tokens": 10 * mult}
        ).execute()


@pytest.fixture
def api(monkeypatch):
    db = FakeClient(org_id=ORG)
    _seed(db)
    for module in (jobs_module, agent2_router, agent3_router):
        monkeypatch.setattr(module, "get_service_role_client", lambda: db)
    app = FastAPI()
    for module in (jobs_module, agent2_router, agent3_router):
        app.include_router(module.router)

    def sign_in(user_id):
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(user_id, ORG, "tok", db)

    monkeypatch.setenv(COST_OPERATOR_ENV, "operator-1, operator-2")
    return TestClient(app), sign_in, db


# --- who may call them --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ENDPOINTS.values())
def test_a_regular_org_member_gets_404_not_403(api, path):
    http, sign_in, _ = api
    sign_in("member")
    response = http.get(path)
    assert response.status_code == 404
    assert "cost" not in response.text.lower() and "operator" not in response.text.lower()


@pytest.mark.parametrize("path", ENDPOINTS.values())
def test_nobody_is_an_operator_until_the_env_var_is_set(api, monkeypatch, path):
    http, sign_in, _ = api
    monkeypatch.delenv(COST_OPERATOR_ENV)
    sign_in("operator-1")
    assert http.get(path).status_code == 404
    monkeypatch.setenv(COST_OPERATOR_ENV, "   ,  ")
    assert http.get(path).status_code == 404


@pytest.mark.parametrize("path", ENDPOINTS.values())
def test_an_operator_gets_200(api, path):
    http, sign_in, _ = api
    sign_in("operator-2")
    assert http.get(path).status_code == 200


def test_operator_ids_are_parsed_forgivingly_and_read_at_call_time(monkeypatch):
    monkeypatch.setenv(COST_OPERATOR_ENV, " a , b,,c ")
    assert operator_user_ids() == {"a", "b", "c"}
    assert is_cost_operator("b") and not is_cost_operator("d") and not is_cost_operator("")
    monkeypatch.setenv(COST_OPERATOR_ENV, "d")
    assert is_cost_operator("d") and not is_cost_operator("a")
    assert cost_access.COST_OPERATOR_ENV == "COST_OPERATOR_USER_IDS"


# --- what they return ----------------------------------------------------------------------------


def test_ingestion_cost_has_totals_stage_purpose_and_cache_counts(api):
    http, sign_in, _ = api
    sign_in("operator-1")
    body = http.get(ENDPOINTS["ingestion"]).json()

    assert body["total"] == {"calls": 5, "error_calls": 0, "unpriced_calls": 0, "tokens": 900, "cost_usd": 0.0125}
    assert [(s["stage"], s["calls"]) for s in body["by_stage"]] == [("fold", 2), ("summarize", 3)]
    assert [p["purpose"] for p in body["by_purpose"]] == ["entity_summary"]
    assert body["cache"] == {"hits": 3, "misses": 2}
    assert "by_unit" not in body


def test_planning_cost_has_no_cache_and_no_units(api):
    http, sign_in, _ = api
    sign_in("operator-1")
    body = http.get(ENDPOINTS["planning"]).json()
    assert body["total"]["calls"] == 5 and "cache" not in body and "by_unit" not in body


def test_execution_cost_adds_a_per_unit_breakdown_with_rounds_and_attempts(api):
    http, sign_in, _ = api
    sign_in("operator-1")
    body = http.get(ENDPOINTS["execution"]).json()
    assert body["by_unit"] == [
        {"cluster_id": "u1", "rounds": 2, "attempts": 3, "calls": 5, "error_calls": 0, "unpriced_calls": 0,
         "tokens": 900, "cost_usd": 0.0125}
    ]


def test_qa_cost_sums_the_questions_asked_against_the_job(api):
    http, sign_in, db = api
    sign_in("operator-1")
    db.table("llm_invocations").insert(
        {"org_id": ORG, "job_type": "qa", "source_job_id": "job-1", "purpose": "qa_answer", "stage": None,
         "status": "error", "cost_usd": None, "total_tokens": None}
    ).execute()
    body = http.get(ENDPOINTS["qa"]).json()
    assert body["total"] == {"calls": 2, "error_calls": 1, "unpriced_calls": 0, "tokens": 10, "cost_usd": 0.001}
    assert [p["purpose"] for p in body["by_purpose"]] == ["qa_answer"]


def test_a_job_with_no_llm_calls_yet_reports_zeros_not_an_error(api):
    http, sign_in, db = api
    sign_in("operator-1")
    for table in ("llm_cost_by_job", "llm_cost_by_job_stage", "llm_cost_by_job_purpose"):
        db.table(table).rows.clear()
    body = http.get(ENDPOINTS["ingestion"]).json()
    assert body["total"] == {"calls": 0, "error_calls": 0, "unpriced_calls": 0, "tokens": 0, "cost_usd": 0.0}
    assert body["by_stage"] == [] and body["by_purpose"] == []


def test_an_unknown_price_makes_the_cost_null_instead_of_a_silently_low_sum(api):
    http, sign_in, db = api
    sign_in("operator-1")
    total = next(r for r in db.table("llm_cost_by_job").rows if r["org_id"] == ORG and r["job_type"] == "ingestion")
    total.update(unpriced_calls=2)
    stage = next(r for r in db.table("llm_cost_by_job_stage").rows if r["org_id"] == ORG and r["job_type"] == "ingestion" and r["stage"] == "fold")
    stage.update(unpriced_calls=1)

    body = http.get(ENDPOINTS["ingestion"]).json()

    assert body["total"]["cost_usd"] is None and body["total"]["unpriced_calls"] == 2
    by_stage = {s["stage"]: s for s in body["by_stage"]}
    assert by_stage["fold"]["cost_usd"] is None and by_stage["summarize"]["cost_usd"] == 0.0125


# --- scoping ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/agent1/jobs/nope/cost", "/api/agent1/jobs/nope/qa-cost",
                                  "/api/agent2/planning-jobs/nope/cost", "/api/agent3/executions/nope/cost"])
def test_an_operator_asking_about_a_missing_job_gets_404(api, path):
    http, sign_in, _ = api
    sign_in("operator-1")
    assert http.get(path).status_code == 404


@pytest.mark.parametrize("path", ENDPOINTS.values())
def test_an_operator_never_sees_another_orgs_numbers_even_for_the_same_job_id(api, path):
    http, sign_in, _ = api
    sign_in("operator-1")  # an operator of ORG: the other org's rows are 100x louder
    body = http.get(path).json()
    assert body["total"]["calls"] < 100 and body["total"]["tokens"] < 1000


def test_an_operator_cannot_read_the_cost_of_a_job_in_another_org(api):
    http, sign_in, db = api
    for table in ("ingestion_jobs", "planning_jobs", "execution_jobs"):
        for row in db.table(table).rows:
            row["org_id"] = OTHER_ORG  # the jobs now belong to someone else
    sign_in("operator-1")
    for path in ENDPOINTS.values():
        assert http.get(path).status_code == 404
