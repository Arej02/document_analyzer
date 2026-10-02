"""PUT/GET/DELETE /api/agent3/projects/{id}/github-connection — design_agent3.md §15.7, §15.10, §15.13
(Phase 7 task 4, backend half).

Same setup as test_agent3_executions_api.py: one FakeClient stands in for both the RLS-scoped and
service-role clients and auth is overridden. GitHub is `fakes_github.FakeGitHub` (a model of documented
behaviour, see that file) and Vault is an in-memory fake that records its calls.
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from document_analyzer.agent3 import connection_router as module
from document_analyzer.agent3.delivery.github_api import GitHubClient
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient
from fakes_github import TOKEN, FakeGitHub, FakeRepo

ORG = "org-123"
REPO = "acme/migrated-shop"
OTHER_TOKEN = "ghp_" + "Z" * 36


class FakeVault:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.events: list[tuple[str, str]] = []
        self.fail_store = False
        self.fail_delete_for: set[str] = set()

    async def store_secret(self, name, description, value):
        if self.fail_store:
            raise RuntimeError("vault is down")
        secret_id = str(uuid.uuid4())
        self.secrets[secret_id] = {"name": name, "description": description, "value": value}
        self.events.append(("store", secret_id))
        return secret_id

    async def delete_secret(self, secret_id):
        if secret_id in self.fail_delete_for:
            raise RuntimeError("cannot delete")
        self.secrets.pop(secret_id, None)
        self.events.append(("delete", secret_id))

    async def resolve_secret(self, secret_id):
        entry = self.secrets.get(secret_id)
        return entry["value"] if entry else None


@pytest.fixture
def fake_client():
    return FakeClient(org_id=ORG)


@pytest.fixture
def github():
    return FakeGitHub(FakeRepo(REPO))


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(module.vault_secrets, "store_secret", fake.store_secret)
    monkeypatch.setattr(module.vault_secrets, "delete_secret", fake.delete_secret)
    monkeypatch.setattr(module.vault_secrets, "resolve_secret", fake.resolve_secret)
    return fake


@pytest.fixture
def client(fake_client, github, vault, monkeypatch):
    monkeypatch.setattr(module, "get_service_role_client", lambda: fake_client)
    monkeypatch.setattr(module, "make_github_client", lambda token: GitHubClient(token, transport=github.transport, sleep=lambda seconds: None))
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


def _client_for(fake_client, github_fake, monkeypatch) -> TestClient:
    monkeypatch.setattr(module, "get_service_role_client", lambda: fake_client)
    monkeypatch.setattr(module, "make_github_client", lambda token: GitHubClient(token, transport=github_fake.transport, sleep=lambda seconds: None))
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=ORG, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def project_id(fake_client):
    return insert_scoped(fake_client, "projects", ORG, {"source_type": "github", "source_ref": "https://x/y"}).data[0]["id"]


def _url(project_id):
    return f"/api/agent3/projects/{project_id}/github-connection"


def _connect(client, project_id, repo=REPO, token=TOKEN):
    return client.put(_url(project_id), json={"repo_full_name": repo, "token": token})


def _rows(fake_client):
    return fake_client.table("project_github_connections").rows


# --------------------------------------------------------------------------- #
# auth and ownership
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["put", "get", "delete"])
def test_missing_auth_returns_401(method):
    response = getattr(TestClient(create_app()), method)("/api/agent3/projects/any/github-connection")

    assert response.status_code == 401


@pytest.mark.parametrize("method", ["put", "get", "delete"])
def test_unknown_and_other_org_projects_are_404(client, fake_client, method):
    other_org_project = insert_scoped(fake_client, "projects", "org-other", {"source_type": "github", "source_ref": "x"}).data[0]["id"]
    kwargs = {"json": {"repo_full_name": REPO, "token": TOKEN}} if method == "put" else {}

    for project in ("does-not-exist", other_org_project):
        assert getattr(client, method)(_url(project), **kwargs).status_code == 404


# --------------------------------------------------------------------------- #
# connect
# --------------------------------------------------------------------------- #


def test_connect_stores_the_connection_and_the_token_only_in_vault(client, fake_client, github, vault, project_id):
    response = _connect(client, project_id)

    assert response.status_code == 200
    assert response.json() == {"connected": True, "repo_full_name": REPO, "default_branch": "main", "warnings": []}
    assert TOKEN not in response.text

    [row] = _rows(fake_client)
    assert (row["project_id"], row["org_id"], row["repo_full_name"], row["default_branch"], row["created_by"]) == (
        project_id, ORG, REPO, "main", "user-1",
    )  # fmt: skip
    [(secret_id, secret)] = vault.secrets.items()
    assert row["token_ref"] == secret_id
    assert secret["value"] == TOKEN
    assert secret["name"].startswith(f"agent3_delivery_token_{project_id}_")
    assert not secret["name"].startswith("agent1_")
    # The token is a column nowhere.
    assert TOKEN not in str(row)
    assert all(request["auth"] == f"Bearer {TOKEN}" for request in github.requests)


def test_get_reports_the_connection_without_any_secret(client, project_id):
    assert client.get(_url(project_id)).json() == {"connected": False}
    _connect(client, project_id)

    body = client.get(_url(project_id)).json()

    assert body["connected"] is True and body["repo_full_name"] == REPO and body["default_branch"] == "main"
    assert set(body) == {"connected", "repo_full_name", "default_branch", "created_at"}
    assert TOKEN not in str(body)


def test_pasted_whitespace_around_the_inputs_is_trimmed(client, vault, project_id):
    response = _connect(client, project_id, repo=f"  {REPO}\n", token=f"{TOKEN}\n")

    assert response.status_code == 200
    assert next(iter(vault.secrets.values()))["value"] == TOKEN


def test_merge_commits_disabled_gives_a_warning_and_still_connects(fake_client, vault, monkeypatch, project_id):
    fake = FakeGitHub(FakeRepo(REPO, allow_merge_commit=False))
    response = _connect(_client_for(fake_client, fake, monkeypatch), project_id)

    assert response.status_code == 200
    assert len(response.json()["warnings"]) == 1 and "merge commit" in response.json()["warnings"][0].lower()
    assert len(_rows(fake_client)) == 1


@pytest.mark.parametrize(
    "repo_kwargs,expected",
    [
        ({"delete_branch_on_merge": False}, "Automatically delete head branches"),
        ({"delete_branch_on_merge": None}, "could not verify"),
        ({"can_push": False}, "cannot push"),
        ({"archived": True}, "archived"),
    ],
)
def test_repo_settings_that_would_break_delivery_are_refused_and_nothing_is_stored(
    fake_client, vault, monkeypatch, project_id, repo_kwargs, expected
):
    fake = FakeGitHub(FakeRepo(REPO, **repo_kwargs))
    response = _connect(_client_for(fake_client, fake, monkeypatch), project_id)

    assert response.status_code == 422
    assert expected.lower() in response.json()["detail"].lower()
    assert TOKEN not in response.text
    assert _rows(fake_client) == [] and vault.secrets == {} and vault.events == []


def test_a_repo_with_more_than_one_branch_is_refused(client, fake_client, github, vault, project_id):
    repo = github.repos[REPO]
    repo.refs.update({"refs/heads/main": "a" * 40, "refs/heads/dev": "b" * 40})

    response = _connect(client, project_id)

    assert response.status_code == 422 and "more than one branch" in response.json()["detail"]
    assert _rows(fake_client) == [] and vault.secrets == {}


def test_a_repo_with_exactly_one_branch_is_accepted(client, github, project_id):
    github.repos[REPO].refs["refs/heads/main"] = "a" * 40

    assert _connect(client, project_id).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"repo_full_name": "../..", "token": TOKEN},
        {"repo_full_name": "a/..", "token": TOKEN},
        {"repo_full_name": "no-slash", "token": TOKEN},
        {"repo_full_name": 5, "token": TOKEN},
        {"repo_full_name": REPO},
        {"repo_full_name": REPO, "token": "short"},
        {"repo_full_name": REPO, "token": "has spaces in it which a token never does"},
        {"repo_full_name": REPO, "token": 12345},
        ["not", "an", "object"],
        None,
    ],
)
def test_malformed_input_is_a_422_that_never_repeats_the_token(client, fake_client, github, vault, project_id, payload):
    response = client.put(_url(project_id), json=payload)

    assert response.status_code == 422
    assert TOKEN not in response.text and "has spaces" not in response.text
    assert github.requests == [] and _rows(fake_client) == [] and vault.secrets == {}


def test_invalid_json_does_not_echo_the_body(client, project_id):
    response = client.put(_url(project_id), content=b'{"repo_full_name": "a/b", "token": "' + TOKEN.encode() + b'"', headers={"content-type": "application/json"})

    assert response.status_code == 422
    assert TOKEN not in response.text


def test_github_rejecting_the_token_is_a_422_and_stores_nothing(client, fake_client, github, vault, project_id):
    response = _connect(client, project_id, token=OTHER_TOKEN)

    assert response.status_code == 422 and "rejected the token" in response.json()["detail"]
    assert OTHER_TOKEN not in response.text
    assert _rows(fake_client) == [] and vault.secrets == {}


def test_an_unknown_repository_is_a_422(client, fake_client, vault, project_id):
    response = _connect(client, project_id, repo="acme/other")

    assert response.status_code == 422 and "not found" in response.json()["detail"]
    assert _rows(fake_client) == [] and vault.secrets == {}


def test_github_being_unavailable_is_a_502_and_stores_nothing(client, fake_client, github, vault, project_id):
    github.fail_next(10, 503)

    response = _connect(client, project_id)

    assert response.status_code == 502
    assert _rows(fake_client) == [] and vault.secrets == {}


def test_a_github_message_echoing_the_token_is_redacted(client, github, project_id):
    github.fail_next(1, 500, message=f"boom {TOKEN}")
    github.fail_next(10, 500, message=f"boom {TOKEN}")

    response = _connect(client, project_id)

    assert TOKEN not in response.text


def test_the_token_is_never_logged(client, github, project_id, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(client, project_id)
    _connect(client, project_id, token=OTHER_TOKEN)
    github.fail_next(10, 500, message=f"boom {TOKEN}")
    _connect(client, project_id)

    assert TOKEN not in caplog.text and OTHER_TOKEN not in caplog.text


def test_a_vault_failure_stores_no_connection(client, fake_client, vault, project_id):
    vault.fail_store = True

    response = _connect(client, project_id)

    assert response.status_code == 500
    assert _rows(fake_client) == []


def test_a_database_failure_removes_the_secret_it_just_created(client, fake_client, vault, project_id, monkeypatch):
    real_table = fake_client.table

    def table(name):
        if name == "project_github_connections":
            raise RuntimeError("database is down")
        return real_table(name)

    # Reads of the connection table are fine until the write; fail only the write.
    monkeypatch.setattr(module, "_get_connection_row", lambda *a, **k: None)
    monkeypatch.setattr(fake_client, "table", table)

    response = _connect(client, project_id)

    assert response.status_code == 500
    assert vault.secrets == {}  # no dangling secret
    assert [event for event, _ in vault.events] == ["store", "delete"]


# --------------------------------------------------------------------------- #
# rotate
# --------------------------------------------------------------------------- #


def test_rotating_to_a_new_token_and_repo_replaces_the_secret_in_order(fake_client, vault, monkeypatch, project_id):
    first = FakeGitHub(FakeRepo(REPO), FakeRepo("acme/second"), token=TOKEN)
    second = FakeGitHub(FakeRepo(REPO), FakeRepo("acme/second"), token=OTHER_TOKEN)
    current = {"fake": first}
    client = _client_for(fake_client, first, monkeypatch)
    monkeypatch.setattr(module, "make_github_client", lambda token: GitHubClient(token, transport=current["fake"].transport, sleep=lambda seconds: None))

    assert _connect(client, project_id).status_code == 200
    [old_ref] = vault.secrets
    current["fake"] = second
    response = _connect(client, project_id, repo="acme/second", token=OTHER_TOKEN)

    assert response.status_code == 200 and response.json()["repo_full_name"] == "acme/second"
    [row] = _rows(fake_client)  # still one connection per project
    [(new_ref, secret)] = vault.secrets.items()
    assert new_ref != old_ref and row["token_ref"] == new_ref and secret["value"] == OTHER_TOKEN
    assert row["repo_full_name"] == "acme/second" and row["created_by"] == "user-1"
    # Order: store(old), store(new), delete(old) — never a moment with the row pointing at nothing.
    assert vault.events == [("store", old_ref), ("store", new_ref), ("delete", old_ref)]


def test_a_failed_delete_of_the_old_secret_does_not_fail_the_rotation(client, fake_client, github, vault, project_id):
    _connect(client, project_id)
    [old_ref] = vault.secrets
    vault.fail_delete_for.add(old_ref)

    response = _connect(client, project_id)

    assert response.status_code == 200
    [row] = _rows(fake_client)
    assert row["token_ref"] != old_ref and row["token_ref"] in vault.secrets  # connection points at the live secret


def test_a_rejected_new_token_leaves_the_existing_connection_untouched(client, fake_client, vault, project_id):
    _connect(client, project_id)
    [(ref, _)] = vault.secrets.items()

    response = _connect(client, project_id, token=OTHER_TOKEN)

    assert response.status_code == 422
    [row] = _rows(fake_client)
    assert row["token_ref"] == ref and list(vault.secrets) == [ref]


# --------------------------------------------------------------------------- #
# active delivery guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["queued", "running"])
def test_connect_and_disconnect_are_refused_while_a_delivery_is_active(client, fake_client, github, vault, project_id, status):
    insert_scoped(fake_client, "deliveries", ORG, {"project_id": project_id, "execution_job_id": "ej-1", "status": status})

    assert _connect(client, project_id).status_code == 409
    assert client.delete(_url(project_id)).status_code == 409
    assert github.requests == [] and vault.events == []  # refused before any GitHub or Vault call


@pytest.mark.parametrize("status", ["done", "failed"])
def test_a_finished_delivery_does_not_block(client, fake_client, project_id, status):
    insert_scoped(fake_client, "deliveries", ORG, {"project_id": project_id, "execution_job_id": "ej-1", "status": status})

    assert _connect(client, project_id).status_code == 200
    assert client.delete(_url(project_id)).status_code == 204


def test_another_projects_active_delivery_does_not_block(client, fake_client, project_id):
    insert_scoped(fake_client, "deliveries", ORG, {"project_id": "someone-elses", "execution_job_id": "ej-9", "status": "running"})

    assert _connect(client, project_id).status_code == 200


# --------------------------------------------------------------------------- #
# disconnect
# --------------------------------------------------------------------------- #


def test_disconnect_removes_the_row_then_the_secret(client, fake_client, vault, project_id):
    _connect(client, project_id)
    [ref] = vault.secrets

    response = client.delete(_url(project_id))

    assert response.status_code == 204 and response.content == b""
    assert _rows(fake_client) == [] and vault.secrets == {}
    assert vault.events[-1] == ("delete", ref)
    assert client.get(_url(project_id)).json() == {"connected": False}


def test_disconnecting_when_not_connected_is_a_no_op(client, vault, project_id):
    assert client.delete(_url(project_id)).status_code == 204
    assert vault.events == []


def test_a_failed_secret_delete_on_disconnect_still_disconnects(client, fake_client, vault, project_id):
    _connect(client, project_id)
    [ref] = vault.secrets
    vault.fail_delete_for.add(ref)

    assert client.delete(_url(project_id)).status_code == 204
    assert _rows(fake_client) == []
