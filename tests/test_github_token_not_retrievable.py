"""tasks.md Phase 6, third task: confirm a github_token_ref secret is not
retrievable through any API once its job has completed.

Two distinct systems are involved, so two distinct checks:
1. No FastAPI endpoint response ever includes `github_token_ref` at all —
   `GET /api/agent1/jobs/{job_id}` and `GET /api/agent1/projects/{project_id}`
   are the only two that return anything about a job, and neither one's
   response shape (jobs.py's `_JOB_STATUS_COLUMNS`, projects.py's
   `_JOB_COLUMNS`) selects the column in the first place — checked here
   against tests/fakes.py::FakeClient, same as the rest of the endpoint test
   suite, since this is static response-shape code, not something RLS or the
   service-role split affects.
2. The Vault secret itself actually becomes unretrievable via
   `resolve_github_token` once `delete_github_token` has run — worker/run.py's
   `process_job` calls it in a `finally` block on every job outcome (success,
   handled failure, or an unexpected exception), so by the time a job is
   `done` or `failed` the secret should already be gone. tests/test_github_token.py
   mocks the asyncpg connection entirely and has never actually exercised
   pgsodium/Vault for real — this is the first test that does. Requires live
   credentials (SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY, via SUPABASE_DB_URL
   for the direct Postgres connection github_token.py uses); skips itself
   otherwise, same as the other two Phase 6 smoke tests.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Call directly (same fix as the other two Phase 6 smoke tests) rather than
# relying on another test module's import order to have populated os.environ.
load_dotenv()

from document_analyzer.app import create_app  # noqa: E402
from document_analyzer.auth import AuthContext, get_auth_context  # noqa: E402
from document_analyzer.github_token import delete_github_token, resolve_github_token, store_github_token  # noqa: E402
from document_analyzer.supabase_client import insert_scoped  # noqa: E402

from fakes import FakeClient  # noqa: E402


@pytest.fixture
def fake_client():
    return FakeClient(org_id="org-123")


@pytest.fixture
def client(fake_client):
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app)


def _seeded_job_with_token_ref(fake_client, *, status: str) -> dict:
    project = insert_scoped(
        fake_client, "projects", fake_client.org_id, {"source_type": "github", "source_ref": "https://github.com/org/private-repo"}
    ).data[0]
    return insert_scoped(
        fake_client,
        "ingestion_jobs",
        fake_client.org_id,
        {
            "project_id": project["id"],
            "source_type": "github",
            "source_ref": project["source_ref"],
            "status": status,
            "github_token_ref": "vault-ref-super-secret-id",
        },
    ).data[0]


@pytest.mark.parametrize("status", ["queued", "running", "done", "failed"])
def test_get_job_never_returns_github_token_ref(client, fake_client, status):
    job = _seeded_job_with_token_ref(fake_client, status=status)

    response = client.get(f"/api/agent1/jobs/{job['id']}")

    assert response.status_code == 200
    body = response.json()
    assert "github_token_ref" not in body
    assert "vault-ref-super-secret-id" not in response.text


def test_get_project_detail_job_history_never_returns_github_token_ref(client, fake_client):
    job = _seeded_job_with_token_ref(fake_client, status="done")

    response = client.get(f"/api/agent1/projects/{job['project_id']}")

    assert response.status_code == 200
    [job_row] = response.json()["jobs"]
    assert "github_token_ref" not in job_row
    assert "vault-ref-super-secret-id" not in response.text


_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL"]


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY/SUPABASE_DB_URL)",
)
async def test_vault_secret_is_unretrievable_after_delete():
    job_id = f"write-path-smoke-{uuid.uuid4().hex}"
    ref = await store_github_token(job_id, "ghp_smoke_test_token")

    # Positive control — proves the ref is genuinely live in Vault first, so
    # "unretrievable after delete" below means the delete worked, not that the
    # ref was never resolvable at all.
    assert await resolve_github_token(ref) == "ghp_smoke_test_token"

    await delete_github_token(ref)

    assert await resolve_github_token(ref) is None
