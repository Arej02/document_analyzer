"""Agent 3 API — POST /api/agent3/plans/{plan_id}/executions (design_agent3.md §9).

Same setup as tests/test_agent2_plans_api.py: one FakeClient stands in for both
the RLS-scoped and service-role clients, and auth is overridden.
"""

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from document_analyzer.agent3 import router as agent3_router_module
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient

ORG = "org-123"
SNAPSHOT = "org-123/job/source.tar.gz"


@pytest.fixture
def fake_client():
    return FakeClient(org_id=ORG)


@pytest.fixture
def client(fake_client, monkeypatch):
    monkeypatch.setattr(agent3_router_module, "get_service_role_client", lambda: fake_client)
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(execution_job_id, org_id):
        calls.append({"execution_job_id": execution_job_id, "org_id": org_id})

    monkeypatch.setattr(agent3_router_module, "enqueue_execution_job", fake_enqueue)
    return calls


def _seed_plan(fake_client, *, org_id=ORG, status="approved", snapshot=SNAPSHOT, items=2, latest_is_source=True):
    """A project, its source job (the latest one unless `latest_is_source=False`), and a plan with `items` units."""
    project = insert_scoped(fake_client, "projects", org_id, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        org_id,
        {"project_id": project["id"], "status": "done", "source_snapshot_path": snapshot},
    ).data[0]
    project["latest_job_id"] = job["id"] if latest_is_source else "newer-job"
    plan = insert_scoped(
        fake_client,
        "migration_plans",
        org_id,
        {"project_id": project["id"], "source_job_id": job["id"], "status": status, "version": 1},
    ).data[0]
    for n in range(items):
        insert_scoped(
            fake_client,
            "migration_plan_items",
            org_id,
            {
                "plan_id": plan["id"],
                "cluster_id": f"unit-{n}",
                "phase_cluster_id": "phase-0",
                "phase_order": 0,
                "unit_order": n,
                "test_contract": {},
            },
        )
    return plan


def test_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent3/plans/any/executions")

    assert response.status_code == 401


def test_happy_path_creates_job_units_and_enqueues(client, fake_client, enqueued, monkeypatch):
    monkeypatch.setenv("AGENT3_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AGENT3_RUNNER_IMAGE", "img@sha256:abc")
    monkeypatch.setenv("AGENT3_MODEL_DEPLOYMENT", "gpt-x")
    plan = _seed_plan(fake_client)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    [job] = fake_client.table("execution_jobs").rows
    assert job["id"] == body["execution_job_id"]
    assert (job["org_id"], job["plan_id"], job["project_id"]) == (ORG, plan["id"], plan["project_id"])
    assert job["source_job_id"] == plan["source_job_id"]
    assert job["status"] == "queued"
    assert job["params"]["max_attempts"] == 5
    assert job["params"]["runner_image"] == "img@sha256:abc"
    assert job["params"]["model_deployment"] == "gpt-x"
    assert set(job["params"]) == {
        "model_deployment", "max_attempts", "max_context_chars", "sandbox_timeout_seconds", "sandbox_memory_mb", "runner_image",
    }

    units = sorted(fake_client.table("unit_executions").rows, key=lambda r: r["unit_order"])
    assert [(u["cluster_id"], u["unit_order"], u["status"]) for u in units] == [("unit-0", 0, "pending"), ("unit-1", 1, "pending")]
    assert all(u["org_id"] == ORG and u["execution_job_id"] == job["id"] and u["phase_cluster_id"] == "phase-0" for u in units)

    assert enqueued == [{"execution_job_id": job["id"], "org_id": ORG}]


def test_params_default_to_design_values(client, fake_client, enqueued, monkeypatch):
    for var in ("AGENT3_MAX_ATTEMPTS", "AGENT3_MAX_CONTEXT_CHARS", "AGENT3_SANDBOX_TIMEOUT_SECONDS", "AGENT3_SANDBOX_MEMORY_MB", "AGENT3_RUNNER_IMAGE"):
        monkeypatch.delenv(var, raising=False)
    plan = _seed_plan(fake_client)

    client.post(f"/api/agent3/plans/{plan['id']}/executions")

    [job] = fake_client.table("execution_jobs").rows
    assert {k: job["params"][k] for k in ("max_attempts", "max_context_chars", "sandbox_timeout_seconds", "sandbox_memory_mb", "runner_image")} == {
        "max_attempts": 3,
        "max_context_chars": 60000,
        "sandbox_timeout_seconds": 120,
        "sandbox_memory_mb": 1024,
        "runner_image": "galvanise-agent3-runner:0.1",
    }


def test_404_for_missing_plan(client, enqueued):
    assert client.post("/api/agent3/plans/nope/executions").status_code == 404
    assert enqueued == []


def test_404_for_other_org_plan(client, fake_client, enqueued):
    plan = _seed_plan(fake_client, org_id="org-other")

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 404
    assert fake_client.table("execution_jobs").rows == []


@pytest.mark.parametrize("status", ["draft", "in_review", "rejected"])
def test_409_when_plan_not_approved(client, fake_client, enqueued, status):
    plan = _seed_plan(fake_client, status=status)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "approve every phase first" in response.json()["detail"]
    assert status in response.json()["detail"]
    assert fake_client.table("execution_jobs").rows == [] and enqueued == []


def test_409_when_plan_went_stale_and_it_gets_marked(client, fake_client, enqueued):
    plan = _seed_plan(fake_client, latest_is_source=False)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert fake_client.table("migration_plans").rows[0]["status"] == "stale"
    assert fake_client.table("execution_jobs").rows == []


def test_409_when_plan_already_marked_stale(client, fake_client, enqueued):
    plan = _seed_plan(fake_client, status="stale")

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]


@pytest.mark.parametrize("snapshot", [None, ""])
def test_409_when_source_job_has_no_snapshot(client, fake_client, enqueued, snapshot):
    plan = _seed_plan(fake_client, snapshot=snapshot)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "re-ingest" in response.json()["detail"]
    assert fake_client.table("execution_jobs").rows == [] and enqueued == []


def test_409_when_plan_has_no_units(client, fake_client, enqueued):
    plan = _seed_plan(fake_client, items=0)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert fake_client.table("execution_jobs").rows == []


@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_409_when_execution_already_active(client, fake_client, enqueued, active_status):
    plan = _seed_plan(fake_client)
    insert_scoped(fake_client, "execution_jobs", ORG, {"plan_id": plan["id"], "status": active_status})

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "already queued or running" in response.json()["detail"]
    assert len(fake_client.table("execution_jobs").rows) == 1 and enqueued == []


@pytest.mark.parametrize("finished_status", ["done", "failed"])
def test_allowed_after_previous_execution_finished(client, fake_client, enqueued, finished_status):
    plan = _seed_plan(fake_client)
    insert_scoped(fake_client, "execution_jobs", ORG, {"plan_id": plan["id"], "status": finished_status})

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 202
    assert len(fake_client.table("execution_jobs").rows) == 2


def test_other_orgs_active_job_does_not_block(client, fake_client, enqueued):
    plan = _seed_plan(fake_client)
    insert_scoped(fake_client, "execution_jobs", "org-other", {"plan_id": plan["id"], "status": "running"})

    assert client.post(f"/api/agent3/plans/{plan['id']}/executions").status_code == 202


def test_409_on_concurrent_insert_unique_violation(client, fake_client, enqueued, monkeypatch):
    plan = _seed_plan(fake_client)

    def racing_insert(*args, **kwargs):
        raise APIError({"code": "23505", "message": "duplicate key value violates unique constraint"})

    monkeypatch.setattr(agent3_router_module, "insert_scoped", racing_insert)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions")

    assert response.status_code == 409
    assert "already queued or running" in response.json()["detail"]
    assert enqueued == []


def test_enqueue_failure_marks_the_job_failed_so_the_plan_is_not_blocked(client, fake_client, monkeypatch):
    plan = _seed_plan(fake_client)

    async def broken_enqueue(execution_job_id, org_id):
        raise ConnectionError("pgmq unreachable")

    monkeypatch.setattr(agent3_router_module, "enqueue_execution_job", broken_enqueue)

    with pytest.raises(ConnectionError):
        client.post(f"/api/agent3/plans/{plan['id']}/executions")

    [job] = fake_client.table("execution_jobs").rows
    assert job["status"] == "failed"
    assert "could not start" in job["error"]


def test_org_id_in_body_or_query_is_ignored(client, fake_client, enqueued):
    plan = _seed_plan(fake_client)

    response = client.post(f"/api/agent3/plans/{plan['id']}/executions?org_id=org-evil", json={"org_id": "org-evil"})

    assert response.status_code == 202
    assert fake_client.table("execution_jobs").rows[0]["org_id"] == ORG
    assert all(u["org_id"] == ORG for u in fake_client.table("unit_executions").rows)
    assert enqueued[0]["org_id"] == ORG
