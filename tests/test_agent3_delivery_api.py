"""POST /executions/{id}/deliver and GET /executions/{id}/delivery — design_agent3.md §15.8, §15.10
(Phase 7 task 6, the endpoints).

TestClient runs a request's BackgroundTasks before it returns, so a 202 here is followed by a finished
delivery, which is what lets these tests check the whole path: refusal rules, the sweep, and the state a poll
returns. GitHub is `fakes_github.FakeGitHub`; Vault is stubbed to hand back the fake's token.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from document_analyzer.agent3 import delivery_router as module
from document_analyzer.agent3.delivery.github_api import GitHubClient
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped

from delivery_world import ORG, REPO, TOKEN, TOKEN_REF, build_world, delivery_row, new_delivery
from fakes_github import FakeGitHub, FakeRepo


def make_client(world, monkeypatch, *, token=TOKEN) -> TestClient:
    async def resolve_secret(ref):
        return token if ref == TOKEN_REF else None

    monkeypatch.setattr(module.vault_secrets, "resolve_secret", resolve_secret)
    monkeypatch.setattr(module, "get_service_role_client", lambda: world.client)
    monkeypatch.setattr(module, "make_github_client", lambda t: GitHubClient(t, transport=world.github.transport, sleep=lambda s: None))
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=ORG, access_token="tok", client=world.client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


def deliver_url(world):
    return f"/api/agent3/executions/{world.execution_job_id}/deliver"


def poll_url(world):
    return f"/api/agent3/executions/{world.execution_job_id}/delivery"


def github_writes(world):
    return [r for r in world.github.requests if r["method"] in ("POST", "PUT", "PATCH")]


def minutes_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).isoformat()


# --------------------------------------------------------------------------- #
# auth and ownership
# --------------------------------------------------------------------------- #


def test_missing_auth_is_401():
    client = TestClient(create_app())

    assert client.post("/api/agent3/executions/x/deliver").status_code == 401
    assert client.get("/api/agent3/executions/x/delivery").status_code == 401


def test_unknown_and_other_org_executions_are_404(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)
    other = insert_scoped(world.client, "execution_jobs", "org-other", {"project_id": "p", "plan_id": "x", "status": "done"}).data[0]["id"]

    for execution_id in ("nope", other):
        assert client.post(f"/api/agent3/executions/{execution_id}/deliver").status_code == 404
        assert client.get(f"/api/agent3/executions/{execution_id}/delivery").status_code == 404


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_starting_a_delivery_returns_202_and_the_poll_shows_the_finished_stack(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)

    response = client.post(deliver_url(world))

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"delivery_id", "status"} and body["status"] == "queued"

    poll = client.get(poll_url(world))
    assert poll.status_code == 200
    [delivery] = poll.json()["deliveries"]
    assert set(delivery) == {"delivery_id", "status", "error", "repo_full_name", "prs", "root_matches_baseline", "created_at"}
    assert delivery["delivery_id"] == body["delivery_id"] and delivery["status"] == "done" and delivery["error"] is None
    assert delivery["repo_full_name"] == REPO
    assert [p["kind"] for p in delivery["prs"]] == ["scaffold", "phase", "phase"]
    assert TOKEN not in poll.text and TOKEN not in response.text and TOKEN_REF not in poll.text


def test_a_failure_inside_the_background_run_is_reported_by_the_poll_not_the_post(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)
    world.github.repos[REPO].refs["refs/heads/other"] = "a" * 40  # a second branch; the default branch "main" is missing

    response = client.post(deliver_url(world))

    assert response.status_code == 202
    [delivery] = client.get(poll_url(world)).json()["deliveries"]
    assert delivery["status"] == "failed" and "other branches" in delivery["error"]


def test_delivering_again_after_success_is_allowed_and_changes_nothing_on_github(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)
    assert client.post(deliver_url(world)).status_code == 202
    world.github.requests.clear()

    assert client.post(deliver_url(world)).status_code == 202

    newest, older = client.get(poll_url(world)).json()["deliveries"]
    assert newest["status"] == older["status"] == "done" and newest["prs"] == older["prs"]
    assert github_writes(world) == []
    assert len(world.github.repos[REPO].pulls) == 3


# --------------------------------------------------------------------------- #
# refusals: each is a 409 before anything is queued or written
# --------------------------------------------------------------------------- #


def _assert_refused(world, client, expected: str):
    before = len(world.client.table("deliveries").rows)
    response = client.post(deliver_url(world))
    assert response.status_code == 409, response.text
    assert expected.lower() in response.json()["detail"].lower()
    assert len(world.client.table("deliveries").rows) == before
    assert github_writes(world) == []


def test_an_execution_that_has_not_finished_is_refused(monkeypatch):
    world = build_world(with_delivery=False, execution_status="running")
    _assert_refused(world, make_client(world, monkeypatch), "not finished")


def test_an_execution_without_output_is_refused(monkeypatch):
    world = build_world(with_delivery=False)
    world.client.table("execution_jobs").rows[0]["output_zip_path"] = None
    _assert_refused(world, make_client(world, monkeypatch), "not finished")


def test_an_execution_without_a_manifest_is_refused(monkeypatch):
    world = build_world(with_delivery=False, with_manifest=False)
    _assert_refused(world, make_client(world, monkeypatch), "no delivery manifest")


def test_a_project_without_a_connection_is_refused(monkeypatch):
    world = build_world(with_delivery=False)
    world.client.table("project_github_connections").rows.clear()
    _assert_refused(world, make_client(world, monkeypatch), "connect a github repository")


def test_a_stale_plan_is_refused_and_existing_prs_are_left_alone(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)
    assert client.post(deliver_url(world)).status_code == 202
    pulls_before = [dict(p) for p in world.github.repos[REPO].pulls]
    world.client.table("projects").rows[0]["latest_job_id"] = "a-newer-ingestion-run"
    world.github.requests.clear()

    _assert_refused(world, client, "plan is stale")

    assert world.github.repos[REPO].pulls == pulls_before  # nothing opened, closed or edited


def test_a_token_github_no_longer_accepts_is_refused(monkeypatch):
    world = build_world(with_delivery=False)
    world.github.token = "ghp_" + "B" * 36  # GitHub now rejects the stored token
    _assert_refused(world, make_client(world, monkeypatch), "no longer accepts the stored token")


def test_branch_deletion_turned_off_since_connecting_is_refused(monkeypatch):
    world = build_world(with_delivery=False, repo=FakeRepo(REPO, delete_branch_on_merge=False))
    _assert_refused(world, make_client(world, monkeypatch), "Automatically delete head branches")


def test_a_vault_secret_that_is_gone_is_refused(monkeypatch):
    world = build_world(with_delivery=False)
    _assert_refused(world, make_client(world, monkeypatch, token=None), "could not be read")


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_second_delivery_while_one_is_active_is_refused(monkeypatch, status):
    world = build_world(with_delivery=False)
    row_id = new_delivery(world.client, world.project_id, world.execution_job_id, status=status)
    delivery_row(world.client, row_id)["heartbeat_at"] = minutes_ago(1)

    _assert_refused(world, make_client(world, monkeypatch), "already queued or running")


def test_losing_the_unique_index_race_is_a_409(monkeypatch):
    world = build_world(with_delivery=False)
    client = make_client(world, monkeypatch)

    def raced(*args, **kwargs):
        raise APIError({"message": "duplicate key value violates unique constraint", "code": "23505", "hint": None, "details": None})

    monkeypatch.setattr(module, "insert_scoped", raced)

    response = client.post(deliver_url(world))

    assert response.status_code == 409 and "already queued or running" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# the stale-delivery sweep (§15.8)
# --------------------------------------------------------------------------- #


def test_a_delivery_with_an_old_heartbeat_is_marked_failed_when_polled(monkeypatch):
    world = build_world(with_delivery=False)
    row_id = new_delivery(world.client, world.project_id, world.execution_job_id, status="running")
    delivery_row(world.client, row_id)["heartbeat_at"] = minutes_ago(20)

    [delivery] = make_client(world, monkeypatch).get(poll_url(world)).json()["deliveries"]

    assert delivery["status"] == "failed" and "interrupted" in delivery["error"]


def test_a_delivery_with_a_recent_heartbeat_is_left_running(monkeypatch):
    world = build_world(with_delivery=False)
    row_id = new_delivery(world.client, world.project_id, world.execution_job_id, status="running")
    delivery_row(world.client, row_id)["heartbeat_at"] = minutes_ago(14)

    [delivery] = make_client(world, monkeypatch).get(poll_url(world)).json()["deliveries"]

    assert delivery["status"] == "running" and delivery["error"] is None


def test_starting_over_an_interrupted_delivery_sweeps_it_then_runs_a_new_one(monkeypatch):
    world = build_world(with_delivery=False)
    stuck = new_delivery(world.client, world.project_id, world.execution_job_id, status="running")
    delivery_row(world.client, stuck)["heartbeat_at"] = minutes_ago(30)
    client = make_client(world, monkeypatch)

    response = client.post(deliver_url(world))

    assert response.status_code == 202
    statuses = {d["delivery_id"]: d["status"] for d in client.get(poll_url(world)).json()["deliveries"]}
    assert statuses[stuck] == "failed" and statuses[response.json()["delivery_id"]] == "done"


def test_the_sweep_only_touches_this_executions_deliveries(monkeypatch):
    world = build_world(with_delivery=False)
    other = new_delivery(world.client, world.project_id, "some-other-execution", status="running")
    delivery_row(world.client, other)["heartbeat_at"] = minutes_ago(60)

    make_client(world, monkeypatch).get(poll_url(world))

    assert delivery_row(world.client, other)["status"] == "running"


def test_polling_lists_deliveries_newest_first(monkeypatch):
    world = build_world(with_delivery=False)
    first = new_delivery(world.client, world.project_id, world.execution_job_id, status="failed")
    second = new_delivery(world.client, world.project_id, world.execution_job_id, status="failed")

    ids = [d["delivery_id"] for d in make_client(world, monkeypatch).get(poll_url(world)).json()["deliveries"]]

    assert ids == [second, first]
