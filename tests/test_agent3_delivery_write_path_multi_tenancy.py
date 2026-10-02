"""Multi-tenancy smoke test (write path) for GitHub delivery — tasks_agent3.md Phase 7 task 8.
Counterpart of `test_agent3_write_path_multi_tenancy.py` (the executor) for `deliveries` and `project_github_connections`.

RLS never applies to the service-role connection the delivery code writes with (CLAUDE.md), so the only protection against a
cross-org write is the code's own discipline:

1. Every write to `deliveries` filters on `id` AND `org_id` (`deliver.py::_update`, the sweep, `_fail`): called with the wrong
   org, it matches zero rows.
2. The `org_id` a row is stamped with comes from the authenticated caller or the trusted delivery row, never from request or
   repository content: a crafted body, or a phase/unit label that is another org's uuid, ends up as plain data only.
3. A project id belonging to another org is a 404 and writes nothing.

Run against a real Postgres (the `deliveries` / `project_github_connections` tables from
`supabase/migrations/20261001100000_agent3_github_delivery.sql` must exist) and the real `agent3-output` bucket. GitHub is the
in-memory fake and Vault is stubbed: only Supabase is real. Requires SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY and
skips itself otherwise. Creates only throwaway rows under two random org ids and removes them afterwards.
"""

import json
import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()  # same fix as the sibling write-path tests: don't depend on another module's import order

from fastapi.testclient import TestClient  # noqa: E402

from document_analyzer.agent3 import connection_router  # noqa: E402
from document_analyzer.agent3.delivery import deliver  # noqa: E402
from document_analyzer.agent3.delivery.deliver import OUTPUT_BUCKET, run_delivery  # noqa: E402
from document_analyzer.agent3.delivery.github_api import GitHubClient  # noqa: E402
from document_analyzer.agent3.delivery_router import sweep_stale_deliveries  # noqa: E402
from document_analyzer.app import create_app  # noqa: E402
from document_analyzer.auth import AuthContext, get_auth_context  # noqa: E402
from document_analyzer.supabase_client import get_service_role_client, insert_scoped  # noqa: E402

from delivery_world import make_zip  # noqa: E402
from fakes_github import TOKEN, FakeGitHub, FakeRepo  # noqa: E402
from test_agent3_commit_plan import _project  # noqa: E402
from test_agent3_write_path_multi_tenancy import _delete_org, _remove_recursively, _seed_execution  # noqa: E402

_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)",
)

REPO = "acme/live-tenancy-check"
TOKEN_REF = str(uuid.uuid4())  # a bare uuid: nothing here reads Vault


def _delivery_tables_cleanup(service, org_id: str) -> None:
    for table in ("deliveries", "project_github_connections"):
        try:
            service.table(table).delete().eq("org_id", org_id).execute()
        except Exception:  # noqa: BLE001 — best effort, keep cleaning
            pass


@pytest.fixture
def two_orgs():
    service = get_service_role_client()
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    seed_a = _seed_execution(service, org_a)
    seed_b = _seed_execution(service, org_b)  # org B owns real rows this time: a project the attacker names
    try:
        yield {"service": service, "org_a": org_a, "org_b": org_b, "a": seed_a, "b": seed_b}
    finally:
        bucket = service.storage.from_(OUTPUT_BUCKET)
        for prefix in (org_a, org_b):
            try:
                if bucket.list(prefix):
                    _remove_recursively(bucket, prefix)
            except Exception:  # noqa: BLE001
                pass
        for org_id in (org_a, org_b):
            _delivery_tables_cleanup(service, org_id)
            _delete_org(service, org_id)


def _delivery(service, org_id: str, seed: dict, **overrides) -> dict:
    return insert_scoped(
        service,
        "deliveries",
        org_id,
        {
            "execution_job_id": seed["execution_job_id"],
            "project_id": seed["project_id"],
            "repo_full_name": REPO,
            "default_branch": "main",
            "status": "queued",
            **overrides,
        },
    ).data[0]


def _row(service, delivery_id: str) -> dict:
    return service.table("deliveries").select("*").eq("id", delivery_id).limit(1).execute().data[0]


def _count(service, table: str, org_id: str) -> int:
    return len(service.table(table).select("*").eq("org_id", org_id).execute().data)


def test_delivery_writes_with_a_mismatched_org_id_are_no_ops(two_orgs):
    service, org_a, org_b = two_orgs["service"], two_orgs["org_a"], two_orgs["org_b"]
    delivery = _delivery(service, org_a, two_orgs["a"], status="running", heartbeat_at="2000-01-01T00:00:00+00:00")

    deliver._update(service, org_b, delivery["id"], status="done")
    deliver._fail(service, org_b, delivery["id"], "wrong org tried to fail it")
    sweep_stale_deliveries(service, org_b, two_orgs["a"]["execution_job_id"])

    untouched = _row(service, delivery["id"])
    assert (untouched["status"], untouched["org_id"], untouched["error"]) == ("running", org_a, None)

    # Positive control: the same calls with the right org do change the row, so the checks above mean something.
    deliver._update(service, org_a, delivery["id"], status="done")
    assert _row(service, delivery["id"])["status"] == "done"


def test_a_crafted_label_that_is_another_orgs_id_stays_plain_data(two_orgs):
    service, org_a, org_b = two_orgs["service"], two_orgs["org_a"], two_orgs["org_b"]
    seed = two_orgs["a"]
    manifest, files = _project()
    for phase in manifest["phases"]:
        phase["label"] = f"phase of {org_b}"
        for unit in phase["units"]:
            unit["label"] = f"unit of {org_b}"
    delivery = _delivery(service, org_a, seed)
    job_id = seed["execution_job_id"]
    zip_path = f"{org_a}/{job_id}/project.zip"
    bucket = service.storage.from_(OUTPUT_BUCKET)
    bucket.upload(zip_path, make_zip(files), {"content-type": "application/zip"})
    bucket.upload(f"{org_a}/{job_id}/manifest.json", json.dumps(manifest).encode("utf-8"), {"content-type": "application/json"})
    service.table("execution_jobs").update({"output_zip_path": zip_path}).eq("id", job_id).eq("org_id", org_a).execute()
    insert_scoped(
        service,
        "project_github_connections",
        org_a,
        {"project_id": seed["project_id"], "repo_full_name": REPO, "default_branch": "main", "token_ref": TOKEN_REF, "created_by": str(uuid.uuid4())},
    )
    github = FakeGitHub(FakeRepo(REPO))

    run_delivery(
        service,
        delivery["id"],
        org_a,
        github_factory=lambda token: GitHubClient(token, transport=github.transport, sleep=lambda s: None),
        resolve_token=lambda ref: TOKEN if ref == TOKEN_REF else None,
    )

    row = _row(service, delivery["id"])
    assert row["status"] == "done", row["error"]
    assert row["org_id"] == org_a
    assert any(org_b in (pr.get("label") or "") for pr in row["prs"])  # the planted id travelled as text...
    for table in ("deliveries", "project_github_connections"):
        assert _count(service, table, org_b) == 0, table  # ...and never became an org_id
    assert bucket.list(org_b) == []


def _client(two_orgs, monkeypatch) -> TestClient:
    service = two_orgs["service"]
    stored = []

    async def store(name, description, value):
        stored.append(name)
        return str(uuid.uuid4())

    async def delete(ref):
        return None

    monkeypatch.setattr(connection_router.vault_secrets, "store_secret", store)
    monkeypatch.setattr(connection_router.vault_secrets, "delete_secret", delete)
    github = FakeGitHub(FakeRepo(REPO))
    monkeypatch.setattr(connection_router, "make_github_client", lambda token: GitHubClient(token, transport=github.transport, sleep=lambda s: None))
    app = create_app()

    async def auth():
        return AuthContext(user_id=str(uuid.uuid4()), org_id=two_orgs["org_a"], access_token="tok", client=service)

    app.dependency_overrides[get_auth_context] = auth
    return TestClient(app, raise_server_exceptions=False)


def test_the_connection_endpoint_stamps_the_callers_org_and_ignores_a_crafted_one(two_orgs, monkeypatch):
    service, org_a, org_b = two_orgs["service"], two_orgs["org_a"], two_orgs["org_b"]
    client = _client(two_orgs, monkeypatch)
    url = f"/api/agent3/projects/{two_orgs['a']['project_id']}/github-connection"

    response = client.put(url, json={"repo_full_name": REPO, "token": TOKEN, "org_id": org_b, "project_id": two_orgs["b"]["project_id"]})

    assert response.status_code == 200, response.text
    rows = service.table("project_github_connections").select("*").execute().data
    mine = [r for r in rows if r["project_id"] == two_orgs["a"]["project_id"]]
    assert len(mine) == 1 and mine[0]["org_id"] == org_a
    assert _count(service, "project_github_connections", org_b) == 0


def test_another_orgs_project_is_a_404_and_nothing_is_written_or_deleted(two_orgs, monkeypatch):
    service, org_a, org_b = two_orgs["service"], two_orgs["org_a"], two_orgs["org_b"]
    client = _client(two_orgs, monkeypatch)
    victim = insert_scoped(
        service,
        "project_github_connections",
        org_b,
        {"project_id": two_orgs["b"]["project_id"], "repo_full_name": REPO, "default_branch": "main", "token_ref": TOKEN_REF, "created_by": str(uuid.uuid4())},
    ).data[0]
    url = f"/api/agent3/projects/{two_orgs['b']['project_id']}/github-connection"

    assert client.put(url, json={"repo_full_name": REPO, "token": TOKEN}).status_code == 404
    assert client.get(url).status_code == 404
    assert client.delete(url).status_code == 404

    survivors = service.table("project_github_connections").select("*").eq("project_id", victim["project_id"]).execute().data
    assert len(survivors) == 1 and survivors[0]["org_id"] == org_b and survivors[0]["token_ref"] == TOKEN_REF
    assert _count(service, "project_github_connections", org_a) == 0
