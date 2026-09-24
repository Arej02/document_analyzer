import copy

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from document_analyzer.agent2 import router as agent2_router_module
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient


@pytest.fixture
def fake_client():
    return FakeClient(org_id="org-123")


@pytest.fixture
def client(fake_client, monkeypatch):
    # One fake stands in for both the RLS-scoped and service-role clients, so the
    # planning_jobs insert is visible to the active-job check on the next request.
    monkeypatch.setattr(agent2_router_module, "get_service_role_client", lambda: fake_client)
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(planning_job_id, org_id):
        calls.append({"planning_job_id": planning_job_id, "org_id": org_id})

    monkeypatch.setattr(agent2_router_module, "enqueue_planning_job", fake_enqueue)
    return calls


def _seed_project_with_job(fake_client, *, org_id="org-123", status="done"):
    project = insert_scoped(
        fake_client, "projects", org_id, {"source_type": "github", "source_ref": "https://github.com/org/repo"}
    ).data[0]
    job = _add_job(fake_client, project, org_id=org_id, status=status)
    project["latest_job_id"] = job["id"]
    return project, job


def _add_job(fake_client, project, *, org_id="org-123", status="done"):
    return insert_scoped(
        fake_client,
        "ingestion_jobs",
        org_id,
        {"project_id": project["id"], "source_type": "github", "source_ref": project["source_ref"], "status": status},
    ).data[0]


def test_create_plan_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent2/projects/any/plans")

    assert response.status_code == 401


def test_create_plan_happy_path_defaults_to_latest_job(client, fake_client, enqueued, monkeypatch):
    monkeypatch.setenv("AGENT2_SEED", "7")
    project, job = _seed_project_with_job(fake_client)

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    [row] = fake_client.table("planning_jobs").rows
    assert row["id"] == body["planning_job_id"]
    assert row["org_id"] == "org-123"
    assert row["project_id"] == project["id"]
    assert row["source_job_id"] == job["id"]
    assert row["status"] == "queued"
    assert row["params"] == {
        "seed": 7,
        "alpha": 0.5,
        "max_unit_size": 25,
        "hub_percentile": 99.0,
        "leiden_resolution": 1.0,
    }
    assert enqueued == [{"planning_job_id": body["planning_job_id"], "org_id": "org-123"}]


def test_create_plan_uses_explicit_source_job_id(client, fake_client, enqueued):
    project, _latest = _seed_project_with_job(fake_client)
    older = _add_job(fake_client, project, status="done")

    response = client.post(f"/api/agent2/projects/{project['id']}/plans", json={"source_job_id": older["id"]})

    assert response.status_code == 202
    [row] = fake_client.table("planning_jobs").rows
    assert row["source_job_id"] == older["id"]


def test_create_plan_409_when_source_job_not_done(client, fake_client, enqueued):
    project, _ = _seed_project_with_job(fake_client, status="running")

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 409
    assert "not done" in response.json()["detail"]
    assert fake_client.table("planning_jobs").rows == []
    assert enqueued == []


def test_create_plan_409_when_project_has_no_ingestion_run(client, fake_client, enqueued):
    project = insert_scoped(
        fake_client, "projects", "org-123", {"source_type": "github", "source_ref": "https://github.com/org/repo"}
    ).data[0]

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 409
    assert enqueued == []


@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_create_plan_409_when_planning_job_already_active(client, fake_client, enqueued, active_status):
    project, job = _seed_project_with_job(fake_client)
    insert_scoped(
        fake_client,
        "planning_jobs",
        "org-123",
        {"project_id": project["id"], "source_job_id": job["id"], "status": active_status, "params": {}},
    )

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 409
    assert "already queued or running" in response.json()["detail"]
    assert len(fake_client.table("planning_jobs").rows) == 1
    assert enqueued == []


def test_create_plan_allowed_after_previous_planning_job_finished(client, fake_client, enqueued):
    project, job = _seed_project_with_job(fake_client)
    insert_scoped(
        fake_client,
        "planning_jobs",
        "org-123",
        {"project_id": project["id"], "source_job_id": job["id"], "status": "failed", "params": {}},
    )

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 202


def test_create_plan_409_on_concurrent_insert_unique_violation(client, fake_client, enqueued, monkeypatch):
    project, _ = _seed_project_with_job(fake_client)

    def racing_insert(*_args, **_kwargs):
        raise APIError({"code": "23505", "message": "duplicate key value violates unique constraint"})

    monkeypatch.setattr(agent2_router_module, "insert_scoped", racing_insert)

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 409
    assert enqueued == []


def test_create_plan_404_for_missing_project(client, enqueued):
    response = client.post("/api/agent2/projects/does-not-exist/plans")

    assert response.status_code == 404
    assert enqueued == []


def test_create_plan_404_for_other_org_project(client, fake_client, enqueued):
    project, _ = _seed_project_with_job(fake_client, org_id="other-org")

    response = client.post(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 404
    assert fake_client.table("planning_jobs").rows == []
    assert enqueued == []


def test_create_plan_404_for_source_job_from_another_project(client, fake_client, enqueued):
    project, _ = _seed_project_with_job(fake_client)
    other_project, other_job = _seed_project_with_job(fake_client)

    response = client.post(f"/api/agent2/projects/{project['id']}/plans", json={"source_job_id": other_job["id"]})

    assert response.status_code == 404
    assert enqueued == []


def _seed_planning_job(fake_client, *, org_id="org-123", status="queued", progress_stage=None, error=None):
    project, job = _seed_project_with_job(fake_client, org_id=org_id)
    return insert_scoped(
        fake_client,
        "planning_jobs",
        org_id,
        {
            "project_id": project["id"],
            "source_job_id": job["id"],
            "status": status,
            "progress_stage": progress_stage,
            "error": error,
            "params": {"seed": 42},
            "graph_stats": {"nodes": 10},
        },
    ).data[0]


def _seed_plan(fake_client, planning_job, *, version, org_id="org-123"):
    return insert_scoped(
        fake_client,
        "migration_plans",
        org_id,
        {
            "project_id": planning_job["project_id"],
            "planning_job_id": planning_job["id"],
            "source_job_id": planning_job["source_job_id"],
            "version": version,
            "status": "draft",
        },
    ).data[0]


def test_get_planning_job_missing_auth_returns_401():
    response = TestClient(create_app()).get("/api/agent2/planning-jobs/any")

    assert response.status_code == 401


def test_get_planning_job_queued(client, fake_client):
    planning_job = _seed_planning_job(fake_client, status="queued")

    response = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "planning_job_id": planning_job["id"],
        "project_id": planning_job["project_id"],
        "status": "queued",
        "progress_stage": None,
        "error": None,
        "plan_id": None,
    }


def test_get_planning_job_running_reports_stage(client, fake_client):
    planning_job = _seed_planning_job(fake_client, status="running", progress_stage="cluster")

    body = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}").json()

    assert body["status"] == "running"
    assert body["progress_stage"] == "cluster"
    assert body["plan_id"] is None


def test_get_planning_job_failed_reports_error(client, fake_client):
    planning_job = _seed_planning_job(fake_client, status="failed", progress_stage="report", error="LLM timeout")

    body = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}").json()

    assert body["status"] == "failed"
    assert body["progress_stage"] == "report"
    assert body["error"] == "LLM timeout"
    assert body["plan_id"] is None


def test_get_planning_job_done_returns_v1_plan_id(client, fake_client):
    planning_job = _seed_planning_job(fake_client, status="done", progress_stage="order")
    v1 = _seed_plan(fake_client, planning_job, version=1)
    _seed_plan(fake_client, planning_job, version=2)  # a later override version must not win

    body = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}").json()

    assert body["status"] == "done"
    assert body["plan_id"] == v1["id"]


def test_get_planning_job_response_never_passes_row_through(client, fake_client):
    planning_job = _seed_planning_job(fake_client, status="running", progress_stage="graph")

    body = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}").json()

    assert set(body) == {"planning_job_id", "project_id", "status", "progress_stage", "error", "plan_id"}


def test_get_planning_job_404_for_missing(client):
    response = client.get("/api/agent2/planning-jobs/does-not-exist")

    assert response.status_code == 404


def test_get_planning_job_404_for_other_org(client, fake_client):
    planning_job = _seed_planning_job(fake_client, org_id="other-org", status="done")
    _seed_plan(fake_client, planning_job, version=1, org_id="other-org")

    response = client.get(f"/api/agent2/planning-jobs/{planning_job['id']}")

    assert response.status_code == 404


def _seed_plan_for_job(fake_client, project, ingestion_job, *, version=1, status="draft", org_id="org-123"):
    planning_job = insert_scoped(
        fake_client,
        "planning_jobs",
        org_id,
        {"project_id": project["id"], "source_job_id": ingestion_job["id"], "status": "done", "params": {}},
    ).data[0]
    return insert_scoped(
        fake_client,
        "migration_plans",
        org_id,
        {
            "project_id": project["id"],
            "planning_job_id": planning_job["id"],
            "source_job_id": ingestion_job["id"],
            "version": version,
            "status": status,
        },
    ).data[0]


def test_list_plans_missing_auth_returns_401():
    response = TestClient(create_app()).get("/api/agent2/projects/any/plans")

    assert response.status_code == 401


def test_list_plans_newest_first_including_every_version(client, fake_client):
    project, job = _seed_project_with_job(fake_client)
    v1 = _seed_plan_for_job(fake_client, project, job, version=1)
    planning_job = {"id": v1["planning_job_id"], "project_id": project["id"], "source_job_id": job["id"]}
    v2 = _seed_plan(fake_client, planning_job, version=2)

    response = client.get(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 200
    plans = response.json()["plans"]
    assert [p["id"] for p in plans] == [v2["id"], v1["id"]]
    assert plans[1] == {
        "id": v1["id"],
        "version": 1,
        "status": "draft",
        "source_job_id": job["id"],
        "is_stale": False,
        "created_at": v1["created_at"],
    }


def test_list_plans_empty(client, fake_client):
    project, _ = _seed_project_with_job(fake_client)

    response = client.get(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 200
    assert response.json() == {"plans": []}


@pytest.mark.parametrize("old_status", ["draft", "in_review", "approved"])
def test_list_plans_marks_stale_after_newer_ingestion_job(client, fake_client, old_status):
    project, old_job = _seed_project_with_job(fake_client)
    old_plan = _seed_plan_for_job(fake_client, project, old_job, status=old_status)
    new_job = _add_job(fake_client, project)
    project["latest_job_id"] = new_job["id"]
    new_plan = _seed_plan_for_job(fake_client, project, new_job)

    plans = {p["id"]: p for p in client.get(f"/api/agent2/projects/{project['id']}/plans").json()["plans"]}

    assert plans[old_plan["id"]]["status"] == "stale"
    assert plans[old_plan["id"]]["is_stale"] is True
    assert old_plan["status"] == "stale"  # persisted, not just reported
    assert plans[new_plan["id"]]["status"] == "draft"
    assert plans[new_plan["id"]]["is_stale"] is False


def test_list_plans_never_marks_rejected_plan_stale(client, fake_client):
    project, old_job = _seed_project_with_job(fake_client)
    rejected = _seed_plan_for_job(fake_client, project, old_job, status="rejected")
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    [plan] = client.get(f"/api/agent2/projects/{project['id']}/plans").json()["plans"]

    assert plan["status"] == "rejected"
    assert plan["is_stale"] is True
    assert rejected["status"] == "rejected"


def test_list_plans_excludes_other_org_and_other_project(client, fake_client):
    project, job = _seed_project_with_job(fake_client)
    mine = _seed_plan_for_job(fake_client, project, job)
    other_project, other_job = _seed_project_with_job(fake_client)
    _seed_plan_for_job(fake_client, other_project, other_job)
    # A stray other-org row under this project id must neither show up nor be touched.
    stray = _seed_plan_for_job(fake_client, project, _add_job(fake_client, project), org_id="other-org")

    plans = client.get(f"/api/agent2/projects/{project['id']}/plans").json()["plans"]

    assert [p["id"] for p in plans] == [mine["id"]]
    assert stray["status"] == "draft"


def test_list_plans_404_for_missing_project(client):
    response = client.get("/api/agent2/projects/does-not-exist/plans")

    assert response.status_code == 404


def test_list_plans_404_for_other_org_project(client, fake_client):
    project, job = _seed_project_with_job(fake_client, org_id="other-org")
    plan = _seed_plan_for_job(fake_client, project, job, org_id="other-org")
    project["latest_job_id"] = _add_job(fake_client, project, org_id="other-org")["id"]

    response = client.get(f"/api/agent2/projects/{project['id']}/plans")

    assert response.status_code == 404
    assert plan["status"] == "draft"  # the stale check never ran against another org's plans


def _seed_full_plan(fake_client, *, org_id="org-123"):
    """Two phases. Phase 1 holds the foundation unit; phase 2 holds units A and B
    (joined by a needs_facade cycle) and a merged_scc unit M built from O1 + O2.
    Items are inserted out of order so the endpoint has to sort them."""
    project, job = _seed_project_with_job(fake_client, org_id=org_id)
    planning_job = insert_scoped(
        fake_client,
        "planning_jobs",
        org_id,
        {"project_id": project["id"], "source_job_id": job["id"], "status": "done", "params": {}},
    ).data[0]

    def cluster(level, kind="community", size=1, label=None):
        return insert_scoped(
            fake_client,
            "clusters",
            org_id,
            {"planning_job_id": planning_job["id"], "level": level, "kind": kind, "size": size, "label": label},
        ).data[0]["id"]

    ids = {
        "P1": cluster("phase", label="Core"),
        "P2": cluster("phase", label="Checkout"),
        "F": cluster("unit", kind="foundation", size=4, label="Shared utils"),
        "A": cluster("unit", size=3, label="Cart"),
        "B": cluster("unit", size=2, label="Pricing"),
        "O1": cluster("unit", size=2, label="Orders"),
        "O2": cluster("unit", size=1, label="Invoices"),
        "M": cluster("unit", kind="merged_scc", size=3),
    }
    planning_job["graph_stats"] = {"merged_into": {ids["O1"]: ids["M"], ids["O2"]: ids["M"]}}

    def report(name, risk, grounding="verified"):
        insert_scoped(
            fake_client,
            "cluster_reports",
            org_id,
            {
                "cluster_id": ids[name],
                "summary_text": f"{name} summary",
                "risk_level": risk,
                "risk_factors": [],
                "grounding_status": grounding,
            },
        )

    report("P1", "low")
    # P2 deliberately has no report: its risk falls back to the rollup of its units.
    report("F", "low")
    report("A", "medium", grounding="unverified")
    report("B", "low")
    report("O1", "high")
    report("O2", "low", grounding="unverified")

    def edge(a, b, level, flag):
        insert_scoped(
            fake_client,
            "cluster_edges",
            org_id,
            {
                "planning_job_id": planning_job["id"],
                "level": level,
                "from_cluster_id": ids[a],
                "to_cluster_id": ids[b],
                "call_count": 1,
                "hook_binding_count": 0,
                "weight": 1.0,
                "flag": flag,
            },
        )

    edge("A", "B", "unit", "needs_facade")
    edge("B", "A", "unit", "needs_facade")
    edge("O1", "O2", "unit", "merged")
    edge("M", "F", "unit", "none")
    edge("P2", "P1", "phase", "none")

    plan = insert_scoped(
        fake_client,
        "migration_plans",
        org_id,
        {
            "project_id": project["id"],
            "planning_job_id": planning_job["id"],
            "source_job_id": job["id"],
            "version": 1,
            "status": "in_review",
            "open_facade_flags": 1,
        },
    ).data[0]
    for name, phase, phase_order, unit_order in [
        ("M", "P2", 2, 3),
        ("A", "P2", 2, 1),
        ("F", "P1", 1, 1),
        ("B", "P2", 2, 2),
    ]:
        insert_scoped(
            fake_client,
            "migration_plan_items",
            org_id,
            {
                "plan_id": plan["id"],
                "cluster_id": ids[name],
                "phase_cluster_id": ids[phase],
                "phase_order": phase_order,
                "unit_order": unit_order,
                "test_contract": {},
            },
        )
    insert_scoped(
        fake_client,
        "phase_approvals",
        org_id,
        {
            "plan_id": plan["id"],
            "phase_cluster_id": ids["P1"],
            "status": "approved",
            "reviewer_id": "user-1",
            "note": "ok",
        },
    )
    return project, plan, ids


def test_get_plan_missing_auth_returns_401():
    response = TestClient(create_app()).get("/api/agent2/plans/any")

    assert response.status_code == 401


def test_get_plan_shape(client, fake_client):
    project, plan, ids = _seed_full_plan(fake_client)

    response = client.get(f"/api/agent2/plans/{plan['id']}")

    assert response.status_code == 200
    body = response.json()
    assert {k: v for k, v in body.items() if k not in ("phases", "facade_cycles")} == {
        "id": plan["id"],
        "project_id": project["id"],
        "version": 1,
        "status": "in_review",
        "source_job_id": plan["source_job_id"],
        "is_stale": False,
        "open_facade_flags": 1,
        "approved_by": None,
        "approved_at": None,
        "parent_plan_id": None,
        "parent_version": None,
    }
    # The seeded A <-> B cycle has no scc_id, so there's nothing to acknowledge it by.
    assert body["facade_cycles"] == [{"level": "unit", "scc_id": None, "cluster_ids": sorted([ids["A"], ids["B"]])}]
    core = body["phases"][0]
    assert set(core) == {
        "phase_cluster_id",
        "phase_order",
        "label",
        "summary_text",
        "risk_level",
        "grounding_status",
        "approval",
        "units",
    }
    assert core["label"] == "Core"
    assert core["summary_text"] == "P1 summary"
    assert core["risk_level"] == "low"
    assert core["grounding_status"] == "verified"
    assert core["approval"]["status"] == "approved"
    assert core["approval"]["note"] == "ok"
    assert core["approval"]["reviewer_id"] == "user-1"
    assert core["units"] == [
        {
            "cluster_id": ids["F"],
            "unit_order": 1,
            "label": "Shared utils",
            "size": 4,
            "risk_level": "low",
            "grounding_status": "verified",
            "flags": ["foundation"],
        }
    ]


def test_get_plan_orders_phases_and_units(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    body = client.get(f"/api/agent2/plans/{plan['id']}").json()

    assert [p["phase_cluster_id"] for p in body["phases"]] == [ids["P1"], ids["P2"]]
    assert [p["phase_order"] for p in body["phases"]] == [1, 2]
    assert [u["cluster_id"] for u in body["phases"][1]["units"]] == [ids["A"], ids["B"], ids["M"]]
    assert [u["unit_order"] for u in body["phases"][1]["units"]] == [1, 2, 3]


def test_get_plan_flags_and_merged_unit_stand_in(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    checkout = client.get(f"/api/agent2/plans/{plan['id']}").json()["phases"][1]
    units = {u["cluster_id"]: u for u in checkout["units"]}

    assert units[ids["A"]]["flags"] == ["needs_facade"]
    assert units[ids["A"]]["grounding_status"] == "unverified"
    assert units[ids["B"]]["flags"] == ["needs_facade"]
    # The merged_scc unit has no report of its own: its originals' reports stand in.
    merged = units[ids["M"]]
    assert merged["flags"] == ["merged"]
    assert merged["label"] == "Invoices + Orders"  # sorted, so it never depends on uuids
    assert merged["size"] == 3
    assert merged["risk_level"] == "high"
    assert merged["grounding_status"] == "unverified"
    # No phase report: risk rolls up from the units, and there's no approval yet.
    assert checkout["summary_text"] is None
    assert checkout["risk_level"] == "high"
    assert checkout["grounding_status"] is None
    assert checkout["approval"] is None


def test_get_plan_override_merged_item_uses_constituent_reports(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    item = next(r for r in fake_client.table("migration_plan_items").rows if r["cluster_id"] == ids["A"])
    item["test_contract"] = {"merged_cluster_ids": [ids["A"], ids["B"]]}
    items = fake_client.table("migration_plan_items").rows
    items[:] = [r for r in items if r["cluster_id"] != ids["B"]]  # B lives on inside A's item

    units = client.get(f"/api/agent2/plans/{plan['id']}").json()["phases"][1]["units"]
    merged = next(u for u in units if u["cluster_id"] == ids["A"])

    assert merged["label"] == "Cart + Pricing"  # every merged unit, not just the one whose id it keeps
    assert merged["size"] == 5
    assert merged["risk_level"] == "medium"
    assert merged["grounding_status"] == "unverified"
    # The A <-> B cycle now sits inside one item, so it no longer needs a facade (§9.2).
    assert merged["flags"] == ["merged"]


def test_get_plan_stale_on_read(client, fake_client):
    project, plan, _ = _seed_full_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    body = client.get(f"/api/agent2/plans/{plan['id']}").json()

    assert body["status"] == "stale"
    assert body["is_stale"] is True
    assert plan["status"] == "stale"  # persisted, not just reported


def test_get_plan_404_for_missing(client):
    response = client.get("/api/agent2/plans/does-not-exist")

    assert response.status_code == 404


def test_get_plan_404_for_other_org(client, fake_client):
    project, plan, _ = _seed_full_plan(fake_client, org_id="other-org")
    project["latest_job_id"] = _add_job(fake_client, project, org_id="other-org")["id"]

    response = client.get(f"/api/agent2/plans/{plan['id']}")

    assert response.status_code == 404
    assert plan["status"] == "in_review"  # the stale check never ran against another org's plan


# --- GET /api/agent2/plans/{plan_id}/units/{cluster_id} ---


def _seed_unit_members(fake_client, plan, ids, *, org_id="org-123"):
    """Entities, members and entity summaries under _seed_full_plan's clusters. Cart
    holds `add` (plus a duplicate `add` collapsed onto it) and `remove`; the merged_scc
    unit M holds its originals' members, as §7 re-points them."""
    job_id = plan["source_job_id"]

    def entity(name, file_path, entity_type="function"):
        return insert_scoped(
            fake_client,
            "extracted_entities",
            org_id,
            {"job_id": job_id, "entity_type": entity_type, "name": name, "file_path": file_path, "raw_facts": {}},
        ).data[0]["id"]

    def member(cluster, entity_id, canonical_id=None):
        insert_scoped(
            fake_client,
            "cluster_members",
            org_id,
            {"cluster_id": ids[cluster], "entity_id": entity_id, "canonical_entity_id": canonical_id or entity_id},
        )

    def summary(entity_id, grounding):
        insert_scoped(
            fake_client,
            "knowledge_summaries",
            org_id,
            {"job_id": job_id, "entity_id": entity_id, "level": "entity", "summary_text": "s", "grounding_status": grounding},
        )

    e = {
        "add": entity("add", "cart.php"),
        "add_dup": entity("add", "cart.php"),
        "remove": entity("remove", "cart.php"),
        "order": entity("Order", "orders.php", entity_type="class"),
        "invoice": entity("invoice", "invoices.php"),
    }
    member("A", e["add"])
    member("A", e["add_dup"], e["add"])
    member("A", e["remove"])
    member("M", e["order"])
    member("M", e["invoice"])
    # `add` has no summary of its own; its duplicate's stands in (§3.2).
    summary(e["add_dup"], "unverified")
    summary(e["remove"], "verified")
    summary(e["order"], "verified")
    return e


def test_get_plan_unit_missing_auth_returns_401():
    response = TestClient(create_app()).get("/api/agent2/plans/any/units/any")

    assert response.status_code == 401


def test_get_plan_unit_shape_for_unit_with_own_report(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    e = _seed_unit_members(fake_client, plan, ids)
    item = next(r for r in fake_client.table("migration_plan_items").rows if r["cluster_id"] == ids["A"])
    item["test_contract"] = {"public_interface": [{"entity_id": e["add"], "name": "add"}], "merged_cluster_ids": []}

    response = client.get(f"/api/agent2/plans/{plan['id']}/units/{ids['A']}")

    assert response.status_code == 200
    body = response.json()
    assert {k: v for k, v in body.items() if k not in ("members", "edges")} == {
        "plan_id": plan["id"],
        "cluster_id": ids["A"],
        "phase_cluster_id": ids["P2"],
        "phase_order": 2,
        "unit_order": 1,
        "kind": "community",
        "label": "Cart",
        "size": 3,
        "risk_level": "medium",
        "grounding_status": "unverified",
        "flags": ["needs_facade"],
        "report": {
            "summary_text": "A summary",
            "risk_level": "medium",
            "risk_factors": [],
            "grounding_status": "unverified",
        },
        "constituent_reports": [],
        "test_contract": item["test_contract"],
    }
    # The duplicate `add` collapses onto its canonical entity, and lends it its grounding.
    assert body["members"] == [
        {"id": e["add"], "entity_type": "function", "name": "add", "file_path": "cart.php", "grounding_status": "unverified"},
        {"id": e["remove"], "entity_type": "function", "name": "remove", "file_path": "cart.php", "grounding_status": "verified"},
    ]
    edges = {(x["from_cluster_id"], x["to_cluster_id"]): x for x in body["edges"]}
    assert set(edges) == {(ids["A"], ids["B"]), (ids["B"], ids["A"])}
    assert edges[(ids["A"], ids["B"])]["direction"] == "outgoing"
    assert edges[(ids["B"], ids["A"])]["direction"] == "incoming"
    assert all(x["flag"] == "needs_facade" for x in body["edges"])
    assert set(body["edges"][0]) == {
        "id", "from_cluster_id", "to_cluster_id", "direction", "call_count", "hook_binding_count", "weight", "flag", "scc_id",
    }


def test_get_plan_unit_merged_scc_shows_constituent_reports(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    e = _seed_unit_members(fake_client, plan, ids)

    body = client.get(f"/api/agent2/plans/{plan['id']}/units/{ids['M']}").json()

    assert body["kind"] == "merged_scc"
    assert body["flags"] == ["merged"]
    assert body["report"] is None
    assert sorted((r["label"], r["risk_level"], r["grounding_status"]) for r in body["constituent_reports"]) == [
        ("Invoices", "low", "unverified"),
        ("Orders", "high", "verified"),
    ]
    assert [m["id"] for m in body["members"]] == [e["invoice"], e["order"]]  # sorted by file path
    assert body["members"][0]["grounding_status"] is None  # no summary at all
    edges = {(x["from_cluster_id"], x["to_cluster_id"]): x for x in body["edges"]}
    # The originals' `merged` edge is kept for audit, alongside the unit's own dependency.
    assert edges[(ids["O1"], ids["O2"])]["direction"] == "internal"
    assert edges[(ids["O1"], ids["O2"])]["flag"] == "merged"
    assert edges[(ids["M"], ids["F"])]["direction"] == "outgoing"
    assert len(edges) == 2


def test_get_plan_unit_override_merge_spans_every_listed_unit(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    e = _seed_unit_members(fake_client, plan, ids)
    item = next(r for r in fake_client.table("migration_plan_items").rows if r["cluster_id"] == ids["A"])
    item["test_contract"] = {"merged_cluster_ids": [ids["A"], ids["M"]]}

    body = client.get(f"/api/agent2/plans/{plan['id']}/units/{ids['A']}").json()

    assert body["report"] is None
    # M is a merged_scc unit with no report of its own (§7): its originals stand in.
    reports = [r["cluster_id"] for r in body["constituent_reports"]]
    assert reports[0] == ids["A"] and sorted(reports[1:]) == sorted([ids["O1"], ids["O2"]])
    assert {m["id"] for m in body["members"]} == {e["add"], e["remove"], e["order"], e["invoice"]}
    # A <-> B stays a cycle edge and M -> F an outgoing one. Only M's own merged
    # O1 -> O2 edge (between its originals) is internal.
    assert {x["direction"] for x in body["edges"]} == {"outgoing", "incoming", "internal"}
    assert [(x["from_cluster_id"], x["to_cluster_id"]) for x in body["edges"] if x["direction"] == "internal"] == [
        (ids["O1"], ids["O2"])
    ]


def test_get_plan_unit_404_for_unit_not_in_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    # O1 is a real cluster of this planning job, but merged away so never a plan item;
    # P1 is a phase, not a unit.
    for cluster_id in (ids["O1"], ids["P1"], "does-not-exist"):
        response = client.get(f"/api/agent2/plans/{plan['id']}/units/{cluster_id}")
        assert response.status_code == 404
        assert response.json()["detail"] == "unit not found in this plan"


def test_get_plan_unit_404_for_missing_plan(client):
    response = client.get("/api/agent2/plans/does-not-exist/units/any")

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"


def test_get_plan_unit_404_for_other_org(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client, org_id="other-org")

    response = client.get(f"/api/agent2/plans/{plan['id']}/units/{ids['A']}")

    assert response.status_code == 404


def test_get_plan_unit_runs_stale_check(client, fake_client):
    project, plan, ids = _seed_full_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = client.get(f"/api/agent2/plans/{plan['id']}/units/{ids['A']}")

    assert response.status_code == 200
    assert plan["status"] == "stale"


# --- GET /api/agent2/plans/{plan_id}/graph ---


def _add_unit_edge(fake_client, plan, from_id, to_id, *, weight=1.0, flag="none", org_id="org-123"):
    insert_scoped(
        fake_client,
        "cluster_edges",
        org_id,
        {
            "planning_job_id": plan["planning_job_id"],
            "level": "unit",
            "from_cluster_id": from_id,
            "to_cluster_id": to_id,
            "call_count": 1,
            "hook_binding_count": 0,
            "weight": weight,
            "flag": flag,
        },
    )


def test_get_plan_graph_missing_auth_returns_401():
    response = TestClient(create_app()).get("/api/agent2/plans/any/graph?level=phase")

    assert response.status_code == 401


def test_get_plan_graph_phase_level(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    response = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=phase")

    assert response.status_code == 200
    assert response.json() == {
        "nodes": [
            {"id": ids["P1"], "label": "Core", "size": 4, "risk_level": "low"},
            # Size sums the phase's units (A 3 + B 2 + M 3); risk rolls up from them.
            {"id": ids["P2"], "label": "Checkout", "size": 8, "risk_level": "high"},
        ],
        "edges": [{"from": ids["P2"], "to": ids["P1"], "weight": 1.0, "flag": "none"}],
    }


def test_get_plan_graph_unit_level(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    body = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=unit").json()

    # Plan order, with the overview's stand-in fields for the merged_scc unit.
    assert body["nodes"] == [
        {"id": ids["F"], "label": "Shared utils", "size": 4, "risk_level": "low"},
        {"id": ids["A"], "label": "Cart", "size": 3, "risk_level": "medium"},
        {"id": ids["B"], "label": "Pricing", "size": 2, "risk_level": "low"},
        {"id": ids["M"], "label": "Invoices + Orders", "size": 3, "risk_level": "high"},
    ]
    # O1 -> O2 (the merged SCC's internal edge) folds into M and is dropped.
    assert sorted((e["from"], e["to"], e["flag"]) for e in body["edges"]) == sorted(
        [
            (ids["A"], ids["B"], "needs_facade"),
            (ids["B"], ids["A"], "needs_facade"),
            (ids["M"], ids["F"], "none"),
        ]
    )
    assert set(body["edges"][0]) == {"from", "to", "weight", "flag"}


def test_get_plan_graph_unit_level_scoped_to_one_phase(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    body = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=unit&phase_id={ids['P2']}").json()

    assert [n["id"] for n in body["nodes"]] == [ids["A"], ids["B"], ids["M"]]
    # M -> F leaves the phase, so it isn't drawn.
    assert {(e["from"], e["to"]) for e in body["edges"]} == {(ids["A"], ids["B"]), (ids["B"], ids["A"])}


def test_get_plan_graph_remaps_and_aggregates_override_merged_edges(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    items = fake_client.table("migration_plan_items").rows
    next(r for r in items if r["cluster_id"] == ids["A"])["test_contract"] = {"merged_cluster_ids": [ids["A"], ids["B"]]}
    items.remove(next(r for r in items if r["cluster_id"] == ids["B"]))
    _add_unit_edge(fake_client, plan, ids["A"], ids["F"], weight=1.0)
    _add_unit_edge(fake_client, plan, ids["B"], ids["F"], weight=2.0)
    _add_unit_edge(fake_client, plan, ids["A"], ids["M"], weight=1.0)
    _add_unit_edge(fake_client, plan, ids["B"], ids["M"], weight=0.5, flag="needs_facade")

    body = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=unit").json()

    assert [n["id"] for n in body["nodes"]] == [ids["F"], ids["A"], ids["M"]]
    edges = {(e["from"], e["to"]): e for e in body["edges"]}
    # A <-> B is now inside one node; B's edges count toward A's, and the strongest flag wins.
    assert set(edges) == {(ids["A"], ids["F"]), (ids["A"], ids["M"]), (ids["M"], ids["F"])}
    assert edges[(ids["A"], ids["F"])] == {"from": ids["A"], "to": ids["F"], "weight": 3.0, "flag": "none"}
    assert edges[(ids["A"], ids["M"])]["weight"] == 1.5
    assert edges[(ids["A"], ids["M"])]["flag"] == "needs_facade"


@pytest.mark.parametrize("query", ["", "?level=cluster", "?level=phase&phase_id=x"])
def test_get_plan_graph_422_for_invalid_query(client, fake_client, query):
    _, plan, _ = _seed_full_plan(fake_client)

    response = client.get(f"/api/agent2/plans/{plan['id']}/graph{query}")

    assert response.status_code == 422


def test_get_plan_graph_404_for_phase_not_in_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    # A unit id is not a phase id.
    response = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=unit&phase_id={ids['A']}")

    assert response.status_code == 404
    assert response.json()["detail"] == "phase not found in this plan"


def test_get_plan_graph_404_for_missing_plan(client):
    response = client.get("/api/agent2/plans/does-not-exist/graph?level=phase")

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"


def test_get_plan_graph_404_for_other_org(client, fake_client):
    _, plan, _ = _seed_full_plan(fake_client, org_id="other-org")

    response = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=phase")

    assert response.status_code == 404


def test_get_plan_graph_runs_stale_check(client, fake_client):
    project, plan, _ = _seed_full_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = client.get(f"/api/agent2/plans/{plan['id']}/graph?level=phase")

    assert response.status_code == 200
    assert plan["status"] == "stale"


# --- POST /api/agent2/plans/{plan_id}/submit ---


def _seed_draft_plan(fake_client, *, org_id="org-123"):
    project, plan, ids = _seed_full_plan(fake_client, org_id=org_id)
    plan["status"] = "draft"
    return project, plan, ids


def test_submit_plan_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent2/plans/any/submit")

    assert response.status_code == 401


def test_submit_plan_moves_draft_to_in_review(client, fake_client):
    _, plan, _ = _seed_draft_plan(fake_client)

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 200
    assert response.json() == {"plan_id": plan["id"], "status": "in_review"}
    assert plan["status"] == "in_review"  # persisted
    assert client.get(f"/api/agent2/plans/{plan['id']}").json()["status"] == "in_review"


@pytest.mark.parametrize("status", ["in_review", "approved", "rejected", "stale"])
def test_submit_plan_409_from_any_other_state(client, fake_client, status):
    _, plan, _ = _seed_draft_plan(fake_client)
    plan["status"] = status

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 409
    assert f"status={status}" in response.json()["detail"]
    assert plan["status"] == status  # untouched


def test_submit_plan_409_when_stale(client, fake_client):
    project, plan, _ = _seed_draft_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert plan["status"] == "stale"  # the stale check ran, and the submit didn't override it


def test_submit_plan_409_when_rejected_plan_is_on_an_older_run(client, fake_client):
    # A rejected plan never moves to `stale`, but it's still refused as stale first.
    project, plan, _ = _seed_draft_plan(fake_client)
    plan["status"] = "rejected"
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert plan["status"] == "rejected"


def test_submit_plan_409_when_no_longer_draft_at_update_time(client, fake_client, monkeypatch):
    _, plan, _ = _seed_draft_plan(fake_client)
    real_load_plan = agent2_router_module.load_plan

    def load_then_race(*args):
        loaded = real_load_plan(*args)
        plan["status"] = "in_review"  # another request submits between the read and the update
        return loaded

    monkeypatch.setattr(agent2_router_module, "load_plan", load_then_race)

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 409
    assert plan["status"] == "in_review"


def test_submit_plan_404_for_missing(client):
    response = client.post("/api/agent2/plans/does-not-exist/submit")

    assert response.status_code == 404


def test_submit_plan_404_for_other_org(client, fake_client):
    _, plan, _ = _seed_draft_plan(fake_client, org_id="other-org")

    response = client.post(f"/api/agent2/plans/{plan['id']}/submit")

    assert response.status_code == 404
    assert plan["status"] == "draft"


# --- POST /api/agent2/plans/{plan_id}/phases/{phase_cluster_id}/approval ---
# _seed_full_plan: an in_review plan with phases P1 (already approved) and P2, and
# open_facade_flags = 1.


def _approvals(fake_client, plan):
    return [r for r in fake_client.table("phase_approvals").rows if r["plan_id"] == plan["id"]]


def _approve(client, plan, phase_id, status="approved", note=None, **extra):
    return client.post(
        f"/api/agent2/plans/{plan['id']}/phases/{phase_id}/approval", json={"status": status, "note": note, **extra}
    )


def test_approve_phase_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent2/plans/any/phases/any/approval", json={"status": "approved"})

    assert response.status_code == 401


def test_approve_phase_records_decision_and_stays_in_review_while_facades_are_open(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    response = _approve(client, plan, ids["P2"], note="checked the cart cycle")

    assert response.status_code == 200
    body = response.json()
    assert body["plan_id"] == plan["id"]
    assert body["phase_cluster_id"] == ids["P2"]
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["note"] == "checked the cart cycle"
    assert body["approval"]["reviewer_id"] == "user-1"
    # Every phase is approved, but one needs_facade cycle is still open.
    assert body["status"] == "in_review"
    assert plan["status"] == "in_review"
    assert plan.get("approved_by") is None


def test_approve_last_phase_with_no_open_facades_approves_the_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    plan["open_facade_flags"] = 0

    response = _approve(client, plan, ids["P2"])

    assert response.json()["status"] == "approved"
    assert plan["status"] == "approved"
    assert plan["approved_by"] == "user-1"
    assert plan["approved_at"]


def test_get_plan_reports_who_approved_it_and_when(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    plan["open_facade_flags"] = 0
    _approve(client, plan, ids["P2"])

    body = client.get(f"/api/agent2/plans/{plan['id']}").json()

    assert body["status"] == "approved"
    assert body["approved_by"] == "user-1"
    assert body["approved_at"] == plan["approved_at"]


def test_approve_phase_stays_in_review_until_every_phase_is_approved(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    plan["open_facade_flags"] = 0
    fake_client.table("phase_approvals").rows.clear()  # P1 not approved yet

    response = _approve(client, plan, ids["P2"])

    assert response.json()["status"] == "in_review"
    assert plan["status"] == "in_review"


def test_reject_phase_rejects_the_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    response = _approve(client, plan, ids["P2"], status="rejected", note="split pricing out first")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert plan["status"] == "rejected"
    assert plan.get("approved_by") is None


def test_approve_phase_changes_an_existing_decision_in_place(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)
    existing = _approvals(fake_client, plan)[0]
    existing["reviewer_id"] = "someone-else"

    response = _approve(client, plan, ids["P1"], status="rejected", note="changed my mind")

    assert response.json()["status"] == "rejected"
    rows = _approvals(fake_client, plan)
    assert len(rows) == 1  # updated, not a second row
    assert rows[0]["id"] == existing["id"]
    assert (rows[0]["status"], rows[0]["note"], rows[0]["reviewer_id"]) == ("rejected", "changed my mind", "user-1")


def test_approve_phase_takes_the_reviewer_from_the_jwt_not_the_body(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    response = _approve(client, plan, ids["P2"], reviewer_id="forged-user")

    assert response.status_code == 200
    row = next(r for r in _approvals(fake_client, plan) if r["phase_cluster_id"] == ids["P2"])
    assert row["reviewer_id"] == "user-1"
    assert row["org_id"] == "org-123"


@pytest.mark.parametrize("status", ["draft", "approved", "rejected", "stale"])
def test_approve_phase_409_unless_in_review(client, fake_client, status):
    _, plan, ids = _seed_full_plan(fake_client)
    plan["status"] = status
    before = len(_approvals(fake_client, plan))

    response = _approve(client, plan, ids["P2"])

    assert response.status_code == 409
    assert f"status={status}" in response.json()["detail"]
    assert plan["status"] == status
    assert len(_approvals(fake_client, plan)) == before  # nothing recorded


def test_approve_phase_409_when_stale(client, fake_client):
    project, plan, ids = _seed_full_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = _approve(client, plan, ids["P2"])

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert plan["status"] == "stale"
    assert [r["phase_cluster_id"] for r in _approvals(fake_client, plan)] == [ids["P1"]]


def test_approve_phase_reports_a_concurrent_status_change(client, fake_client, monkeypatch):
    _, plan, ids = _seed_full_plan(fake_client)
    plan["open_facade_flags"] = 0
    real_record = agent2_router_module._record_phase_approval

    def record_then_race(*args):
        approval = real_record(*args)
        plan["status"] = "rejected"  # another reviewer's rejection lands first
        return approval

    monkeypatch.setattr(agent2_router_module, "_record_phase_approval", record_then_race)

    response = _approve(client, plan, ids["P2"])

    assert response.json()["status"] == "rejected"
    assert plan.get("approved_by") is None  # the approval transition didn't overwrite it


def test_approve_phase_404_for_phase_not_in_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client)

    # A unit id isn't a phase, and O1 was merged away; neither is a phase of this plan.
    for phase_id in (ids["A"], ids["O1"], "does-not-exist"):
        response = _approve(client, plan, phase_id)
        assert response.status_code == 404
        assert response.json()["detail"] == "phase not found in this plan"
    assert len(_approvals(fake_client, plan)) == 1


def test_approve_phase_404_for_missing_plan(client):
    response = client.post("/api/agent2/plans/does-not-exist/phases/any/approval", json={"status": "approved"})

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"


def test_approve_phase_404_for_other_org(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client, org_id="other-org")

    response = _approve(client, plan, ids["P2"])

    assert response.status_code == 404
    assert plan["status"] == "in_review"


@pytest.mark.parametrize("payload", [{"status": "maybe"}, {}, {"note": "no status"}])
def test_approve_phase_422_for_invalid_body(client, fake_client, payload):
    _, plan, ids = _seed_full_plan(fake_client)

    response = client.post(f"/api/agent2/plans/{plan['id']}/phases/{ids['P2']}/approval", json=payload)

    assert response.status_code == 422


def test_approve_phase_concurrent_first_decision_falls_back_to_update(client, fake_client, monkeypatch):
    _, plan, ids = _seed_full_plan(fake_client)
    table = fake_client.table("phase_approvals")

    class LosesTheRace:
        """Another reviewer's first decision lands between our lookup and our insert."""

        def __init__(self, row):
            self.row = row

        def execute(self):
            table.rows.append({**self.row, "id": "theirs", "status": "rejected", "reviewer_id": "user-2"})
            raise APIError({"code": "23505", "message": "duplicate key value violates unique constraint"})

    monkeypatch.setattr(table, "insert", LosesTheRace)

    response = _approve(client, plan, ids["P2"], note="mine")

    assert response.status_code == 200
    row = next(r for r in _approvals(fake_client, plan) if r["phase_cluster_id"] == ids["P2"])
    assert (row["id"], row["status"], row["reviewer_id"], row["note"]) == ("theirs", "approved", "user-1", "mine")


# --- POST /plans/{plan_id}/overrides (design_agent2.md §9.2, §11) ---


def _seed_override_plan(fake_client, *, status="draft"):
    """_seed_full_plan, made recomputable: the A <-> B cycle carries scc_id 1, each
    unit has members (fixed canonical ids, so the §8 tie key is stable), and A, B and
    M carry version 1 test contracts. Recomputed from scratch, phase 2 orders B, A
    (their cycle, low risk first), then M (high risk)."""
    project, plan, ids = _seed_full_plan(fake_client)
    plan["status"] = status
    for row in fake_client.table("cluster_edges").rows:
        if row["flag"] == "needs_facade":
            row["scc_id"] = 1
    for name, canonical in [("F", "e-f"), ("A", "e-a"), ("B", "e-b"), ("M", "e-m")]:
        insert_scoped(
            fake_client,
            "cluster_members",
            "org-123",
            {"cluster_id": ids[name], "entity_id": canonical, "canonical_entity_id": canonical},
        )
    contracts = {
        "A": {
            "public_interface": [{"entity_id": "e-a", "name": "add"}],
            "incoming_call_sites": [
                {"from_unit_id": ids["B"], "from_entity_id": "e-b", "to_entity_id": "e-a", "call_count": 2}
            ],
            "outgoing_dependencies": [{"to_unit_id": ids["B"], "call_count": 1, "hook_binding_count": 0}],
            "external_calls": ["wp_remote_get"],
            "hooks_bound": ["init"],
            "side_effect_signals": [],
            "unverified_member_ids": ["e-a"],
            "merged_cluster_ids": [],
        },
        "B": {
            "public_interface": [{"entity_id": "e-b", "name": "price"}],
            "incoming_call_sites": [
                {"from_unit_id": ids["A"], "from_entity_id": "e-a", "to_entity_id": "e-b", "call_count": 1}
            ],
            "outgoing_dependencies": [{"to_unit_id": ids["A"], "call_count": 2, "hook_binding_count": 0}],
            "external_calls": ["update_post_meta", "wp_remote_get"],
            "hooks_bound": [],
            "side_effect_signals": [],
            "unverified_member_ids": [],
            "merged_cluster_ids": [],
        },
        "M": {
            "public_interface": [],
            "incoming_call_sites": [],
            "outgoing_dependencies": [{"to_unit_id": ids["F"], "call_count": 1, "hook_binding_count": 0}],
            "external_calls": [],
            "hooks_bound": [],
            "side_effect_signals": [],
            "unverified_member_ids": [],
            "merged_cluster_ids": [],
        },
    }
    for row in fake_client.table("migration_plan_items").rows:
        for name, contract in contracts.items():
            if row["cluster_id"] == ids[name]:
                row["test_contract"] = contract
    return project, plan, ids


def _add_phase2_unit(fake_client, plan, ids, name, *, label, risk="low", edges=()):
    """A unit in phase 2 of plan version 1, with unit edges given as (from, to) names."""
    ids[name] = insert_scoped(
        fake_client,
        "clusters",
        "org-123",
        {"planning_job_id": plan["planning_job_id"], "level": "unit", "kind": "community", "size": 1, "label": label},
    ).data[0]["id"]
    insert_scoped(
        fake_client,
        "cluster_reports",
        "org-123",
        {"cluster_id": ids[name], "summary_text": "s", "risk_level": risk, "risk_factors": [], "grounding_status": "verified"},
    )
    insert_scoped(
        fake_client,
        "cluster_members",
        "org-123",
        {"cluster_id": ids[name], "entity_id": f"e-{name}", "canonical_entity_id": f"e-{name}"},
    )
    insert_scoped(
        fake_client,
        "migration_plan_items",
        "org-123",
        {
            "plan_id": plan["id"],
            "cluster_id": ids[name],
            "phase_cluster_id": ids["P2"],
            "phase_order": 2,
            "unit_order": 4,
            "test_contract": {},
        },
    )
    for a, b in edges:
        insert_scoped(
            fake_client,
            "cluster_edges",
            "org-123",
            {
                "planning_job_id": plan["planning_job_id"],
                "level": "unit",
                "from_cluster_id": ids[a],
                "to_cluster_id": ids[b],
                "call_count": 1,
                "hook_binding_count": 0,
                "weight": 1.0,
                "flag": "none",
            },
        )


def _override(client, plan_id, kind, payload):
    return client.post(f"/api/agent2/plans/{plan_id}/overrides", json={"kind": kind, "payload": payload})


def _plan_row(fake_client, plan_id):
    return next(p for p in fake_client.table("migration_plans").rows if p["id"] == plan_id)


def _items(fake_client, plan_id):
    """(phase_order, unit_order) -> item row."""
    return {
        (r["phase_order"], r["unit_order"]): r
        for r in fake_client.table("migration_plan_items").rows
        if r["plan_id"] == plan_id
    }


def _snapshot(fake_client, plan_id):
    return copy.deepcopy((_plan_row(fake_client, plan_id), _items(fake_client, plan_id)))


def test_override_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent2/plans/any/overrides", json={"kind": "merge", "payload": {}})

    assert response.status_code == 401


def test_facade_ack_writes_the_next_version_and_clears_the_flag(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    before = _snapshot(fake_client, plan["id"])

    response = _override(client, plan["id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "wrap pricing"})

    assert response.status_code == 201
    body = response.json()
    assert (body["version"], body["parent_plan_id"], body["parent_version"]) == (2, plan["id"], 1)
    assert (body["status"], body["open_facade_flags"]) == ("draft", 0)
    new = _plan_row(fake_client, body["plan_id"])
    assert (new["org_id"], new["version"], new["parent_plan_id"], new["status"]) == ("org-123", 2, plan["id"], "draft")
    assert (new["planning_job_id"], new["source_job_id"]) == (plan["planning_job_id"], plan["source_job_id"])
    assert new["open_facade_flags"] == 0
    # Version 1 is untouched.
    assert _snapshot(fake_client, plan["id"]) == before
    # The override is recorded against version 1, pointing at the version it produced.
    [override] = fake_client.table("plan_overrides").rows
    assert override["id"] == body["override_id"]
    assert (override["org_id"], override["plan_id"], override["kind"], override["created_by"]) == (
        "org-123",
        plan["id"],
        "facade_ack",
        "user-1",
    )
    assert override["payload"] == {"level": "unit", "scc_id": 1, "note": "wrap pricing"}
    assert override["result_plan_id"] == body["plan_id"]
    # Same units, recomputed order (B, A, M in phase 2), contracts carried over.
    items = _items(fake_client, body["plan_id"])
    assert {k: r["cluster_id"] for k, r in items.items()} == {
        (1, 1): ids["F"],
        (2, 1): ids["B"],
        (2, 2): ids["A"],
        (2, 3): ids["M"],
    }
    assert all(r["org_id"] == "org-123" for r in items.values())
    assert items[(2, 2)]["test_contract"] == _items(fake_client, plan["id"])[(2, 1)]["test_contract"]


def test_acknowledged_cycle_no_longer_flags_its_units(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    new_id = _override(client, plan["id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "ok"}).json()["plan_id"]

    old_units = client.get(f"/api/agent2/plans/{plan['id']}").json()["phases"][1]["units"]
    new_units = client.get(f"/api/agent2/plans/{new_id}").json()["phases"][1]["units"]

    assert "needs_facade" in next(u for u in old_units if u["cluster_id"] == ids["A"])["flags"]
    assert all("needs_facade" not in u["flags"] for u in new_units)
    detail = client.get(f"/api/agent2/plans/{new_id}/units/{ids['A']}").json()
    assert "needs_facade" not in detail["flags"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"level": "unit", "scc_id": 1}, "note"),
        ({"level": "unit", "scc_id": 1, "note": "  "}, "note"),
        ({"level": "unit", "scc_id": 7, "note": "x"}, "no needs_facade cycle with scc_id 7"),
        ({"level": "phase", "scc_id": 1, "note": "x"}, "no needs_facade cycle"),
        ({"level": "module", "scc_id": 1, "note": "x"}, "level"),
        ({"level": "unit", "scc_id": "1", "note": "x"}, "scc_id"),
    ],
)
def test_facade_ack_422_for_invalid_payload(client, fake_client, payload, message):
    _, plan, _ = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "facade_ack", payload)

    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert len(fake_client.table("migration_plans").rows) == 1
    assert fake_client.table("plan_overrides").rows == []


def test_merge_combines_units_and_their_contracts(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "merge", {"cluster_ids": [ids["B"], ids["A"]]})

    assert response.status_code == 201
    body = response.json()
    # A merge that takes in the whole A <-> B cycle leaves nothing to put a facade on.
    assert body["open_facade_flags"] == 0
    items = _items(fake_client, body["plan_id"])
    merged_id = min(ids["A"], ids["B"])
    assert {k: r["cluster_id"] for k, r in items.items()} == {(1, 1): ids["F"], (2, 1): merged_id, (2, 2): ids["M"]}
    contract = items[(2, 1)]["test_contract"]
    assert contract["merged_cluster_ids"] == sorted([ids["A"], ids["B"]])
    # Calls between A and B are now internal to the item.
    assert contract["incoming_call_sites"] == []
    assert contract["outgoing_dependencies"] == []
    assert contract["external_calls"] == ["update_post_meta", "wp_remote_get"]
    assert contract["hooks_bound"] == ["init"]
    assert [p["entity_id"] for p in contract["public_interface"]] == ["e-a", "e-b"]
    assert contract["unverified_member_ids"] == ["e-a"]

    units = client.get(f"/api/agent2/plans/{body['plan_id']}").json()["phases"][1]["units"]
    unit = next(u for u in units if u["cluster_id"] == merged_id)
    assert unit["flags"] == ["merged"]
    assert unit["size"] == 5


def test_merge_remaps_other_units_references_to_the_merged_item(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    _add_phase2_unit(fake_client, plan, ids, "C", label="Coupons", edges=[("C", "B")])
    item_c = next(r for r in fake_client.table("migration_plan_items").rows if r["cluster_id"] == ids["C"])
    item_c["test_contract"] = {
        "outgoing_dependencies": [{"to_unit_id": ids["B"], "call_count": 4, "hook_binding_count": 1}],
        "incoming_call_sites": [],
    }

    body = _override(client, plan["id"], "merge", {"cluster_ids": [ids["A"], ids["B"]]}).json()

    merged_id = min(ids["A"], ids["B"])
    new_c = next(r for r in _items(fake_client, body["plan_id"]).values() if r["cluster_id"] == ids["C"])
    assert new_c["test_contract"]["outgoing_dependencies"] == [
        {"to_unit_id": merged_id, "call_count": 4, "hook_binding_count": 1}
    ]


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["A"], "two or more"),
        (["A", "A"], "two or more"),
        (["A", "F"], "same phase"),
        (["A", "nope"], "not a unit of this plan version: nope"),
    ],
)
def test_merge_422_for_invalid_payload(client, fake_client, names, message):
    _, plan, ids = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "merge", {"cluster_ids": [ids.get(n, n) for n in names]})

    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert fake_client.table("plan_overrides").rows == []


def test_merge_422_when_it_would_close_a_new_cycle(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    # M -> C -> A: merging A and M would make A+M depend on C and C on A+M.
    _add_phase2_unit(fake_client, plan, ids, "C", label="Coupons", edges=[("M", "C"), ("C", "A")])

    response = _override(client, plan["id"], "merge", {"cluster_ids": [ids["A"], ids["M"]]})

    assert response.status_code == 422
    assert "new dependency cycle" in response.json()["detail"]
    assert "Coupons" in response.json()["detail"]
    assert len(fake_client.table("migration_plans").rows) == 1


def test_reorder_pins_a_unit_position(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 201
    items = _items(fake_client, response.json()["plan_id"])
    assert [items[(2, n)]["cluster_id"] for n in (1, 2, 3)] == [ids["M"], ids["B"], ids["A"]]


def test_reorder_422_when_a_dependency_would_come_later(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    _add_phase2_unit(fake_client, plan, ids, "C", label="Coupons", edges=[("C", "A")])

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["C"], "unit_order": 1})

    assert response.status_code == 422
    assert "“Coupons” depends on “Cart”" in response.json()["detail"]
    assert fake_client.table("plan_overrides").rows == []


@pytest.mark.parametrize("position", [0, 4, "1"])
def test_reorder_422_for_invalid_position(client, fake_client, position):
    _, plan, ids = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": position})

    assert response.status_code == 422


def test_split_is_refused(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)

    response = _override(client, plan["id"], "split", {"cluster_id": ids["A"], "entity_ids": ["e-a"]})

    assert response.status_code == 422
    assert "split overrides aren't part of the MVP" in response.json()["detail"]


def test_override_422_for_unknown_kind_or_missing_payload(client, fake_client):
    _, plan, _ = _seed_override_plan(fake_client)

    assert _override(client, plan["id"], "rename", {}).status_code == 422
    assert client.post(f"/api/agent2/plans/{plan['id']}/overrides", json={"kind": "merge"}).status_code == 422


def test_chained_override_carries_earlier_overrides_forward(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    v2 = _override(client, plan["id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "ok"}).json()
    v2_before = _snapshot(fake_client, v2["plan_id"])

    response = _override(client, v2["plan_id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 201
    v3 = response.json()
    assert (v3["version"], v3["parent_plan_id"], v3["parent_version"]) == (3, v2["plan_id"], 2)
    # v1's acknowledgement still holds on v3, and the reorder applied on top.
    assert v3["open_facade_flags"] == 0
    items = _items(fake_client, v3["plan_id"])
    assert [items[(2, n)]["cluster_id"] for n in (1, 2, 3)] == [ids["M"], ids["B"], ids["A"]]
    assert _snapshot(fake_client, v2["plan_id"]) == v2_before
    # Acknowledging the same cycle again on v3 is refused.
    again = _override(client, v3["plan_id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "again"})
    assert again.status_code == 422
    assert "already acknowledged" in again.json()["detail"]


def test_chained_merge_after_reorder_keeps_ids_stable(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    v2 = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1}).json()

    v3 = _override(client, v2["plan_id"], "merge", {"cluster_ids": [ids["A"], ids["B"]]}).json()

    items = _items(fake_client, v3["plan_id"])
    assert [items[(2, n)]["cluster_id"] for n in (1, 2)] == [ids["M"], min(ids["A"], ids["B"])]


def test_two_overrides_on_the_same_version_branch_independently(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    v2 = _override(client, plan["id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "ok"}).json()
    v3 = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1}).json()

    assert (v2["version"], v3["version"]) == (2, 3)
    assert v3["parent_plan_id"] == plan["id"]
    # v3 was made from v1, so v2's acknowledgement isn't part of it.
    assert v3["open_facade_flags"] == 1
    v4 = _override(client, v3["plan_id"], "facade_ack", {"level": "unit", "scc_id": 1, "note": "this branch too"})
    assert v4.status_code == 201


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_override_409_unless_draft_or_in_review(client, fake_client, status):
    _, plan, ids = _seed_override_plan(fake_client, status=status)

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 409
    assert f"status={status}" in response.json()["detail"]
    assert len(fake_client.table("migration_plans").rows) == 1


def test_override_allowed_in_review(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client, status="in_review")

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 201
    # The new version starts over as a draft; the reviewed version keeps its status.
    assert response.json()["status"] == "draft"
    assert plan["status"] == "in_review"


def test_override_409_when_stale(client, fake_client):
    project, plan, ids = _seed_override_plan(fake_client)
    project["latest_job_id"] = _add_job(fake_client, project)["id"]

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert fake_client.table("plan_overrides").rows == []


def test_override_404_for_missing_plan(client):
    assert _override(client, "nope", "merge", {"cluster_ids": ["a", "b"]}).status_code == 404


def test_override_404_for_other_org_plan(client, fake_client):
    _, plan, ids = _seed_full_plan(fake_client, org_id="other-org")

    response = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert response.status_code == 404
    assert fake_client.table("plan_overrides").rows == []


def test_failed_override_insert_leaves_no_orphan_version(client, fake_client, monkeypatch):
    _, plan, ids = _seed_override_plan(fake_client)

    def rls_rejects(_row):
        raise APIError({"code": "42501", "message": "new row violates row-level security policy"})

    monkeypatch.setattr(fake_client.table("plan_overrides"), "insert", rls_rejects)

    with pytest.raises(APIError):
        _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1})

    assert [p["id"] for p in fake_client.table("migration_plans").rows] == [plan["id"]]
    assert {r["plan_id"] for r in fake_client.table("migration_plan_items").rows} == {plan["id"]}


def test_get_plan_lists_open_facade_cycles_until_acknowledged_or_merged(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    # A phase-level cycle too: P1 <-> P2.
    for a, b in [("P1", "P2"), ("P2", "P1")]:
        insert_scoped(
            fake_client,
            "cluster_edges",
            "org-123",
            {"planning_job_id": plan["planning_job_id"], "level": "phase", "from_cluster_id": ids[a],
             "to_cluster_id": ids[b], "call_count": 1, "hook_binding_count": 0, "weight": 1.0,
             "flag": "needs_facade", "scc_id": 2},
        )
    unit_cycle = {"level": "unit", "scc_id": 1, "cluster_ids": sorted([ids["A"], ids["B"]])}
    phase_cycle = {"level": "phase", "scc_id": 2, "cluster_ids": sorted([ids["P1"], ids["P2"]])}

    assert client.get(f"/api/agent2/plans/{plan['id']}").json()["facade_cycles"] == [phase_cycle, unit_cycle]

    acked = _override(client, plan["id"], "facade_ack", {"level": "phase", "scc_id": 2, "note": "ok"}).json()
    assert client.get(f"/api/agent2/plans/{acked['plan_id']}").json()["facade_cycles"] == [unit_cycle]

    merged = _override(client, acked["plan_id"], "merge", {"cluster_ids": [ids["A"], ids["B"]]}).json()
    assert merged["open_facade_flags"] == 0
    assert client.get(f"/api/agent2/plans/{merged['plan_id']}").json()["facade_cycles"] == []


def test_get_plan_names_the_version_it_was_made_from(client, fake_client):
    _, plan, ids = _seed_override_plan(fake_client)
    v2 = _override(client, plan["id"], "reorder", {"cluster_id": ids["M"], "unit_order": 1}).json()

    body = client.get(f"/api/agent2/plans/{v2['plan_id']}").json()

    assert (body["version"], body["parent_plan_id"], body["parent_version"]) == (2, plan["id"], 1)
