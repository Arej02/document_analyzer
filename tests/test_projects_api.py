import pytest
from fastapi.testclient import TestClient

from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped
from document_analyzer import jobs as jobs_module

from fakes import FakeClient


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


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(job_id, org_id):
        calls.append({"job_id": job_id, "org_id": org_id})

    monkeypatch.setattr(jobs_module, "enqueue_ingestion_job", fake_enqueue)
    return calls


def _seed_project_with_job(fake_client, *, org_id="org-123", status="running"):
    project = insert_scoped(
        fake_client,
        "projects",
        org_id,
        {"source_type": "github", "source_ref": "https://github.com/org/repo"},
    ).data[0]
    job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        org_id,
        {"project_id": project["id"], "source_type": "github", "source_ref": project["source_ref"], "status": status},
    ).data[0]
    project["latest_job_id"] = job["id"]
    for row in fake_client.table("projects").rows:
        if row["id"] == project["id"]:
            row["latest_job_id"] = job["id"]
    return project, job


def test_get_projects_missing_auth_returns_401():
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/agent1/projects")

    assert response.status_code == 401


def test_get_projects_returns_empty_list_when_none_exist(client):
    response = client.get("/api/agent1/projects")

    assert response.status_code == 200
    assert response.json() == []


def test_get_projects_includes_latest_job_status(client, fake_client):
    project, job = _seed_project_with_job(fake_client, status="running")

    response = client.get("/api/agent1/projects")

    assert response.status_code == 200
    [row] = response.json()
    assert row["id"] == project["id"]
    assert row["latest_job_id"] == job["id"]
    assert row["latest_job_status"] == "running"


def test_get_projects_scoped_to_caller_org(client, fake_client):
    _seed_project_with_job(fake_client, org_id="org-123")
    _seed_project_with_job(fake_client, org_id="other-org")

    response = client.get("/api/agent1/projects")

    assert len(response.json()) == 1


def test_get_project_detail_returns_job_history_most_recent_first(client, fake_client):
    project, first_job = _seed_project_with_job(fake_client, status="done")
    second_job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        "org-123",
        {"project_id": project["id"], "source_type": "github", "source_ref": project["source_ref"], "status": "queued"},
    ).data[0]

    response = client.get(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == project["id"]
    assert [j["id"] for j in body["jobs"]] == [second_job["id"], first_job["id"]]


def test_get_project_detail_job_history_includes_diff_counts(client, fake_client):
    project, job = _seed_project_with_job(fake_client, status="done")
    for row in fake_client.table("ingestion_jobs").rows:
        if row["id"] == job["id"]:
            row.update({"changed_file_count": 3, "unchanged_file_count": 12, "removed_file_count": 0})

    response = client.get(f"/api/agent1/projects/{project['id']}")

    [job_row] = response.json()["jobs"]
    assert job_row["changed_file_count"] == 3
    assert job_row["unchanged_file_count"] == 12
    assert job_row["removed_file_count"] == 0


def test_get_project_detail_404_for_unknown_project(client):
    response = client.get("/api/agent1/projects/does-not-exist")

    assert response.status_code == 404


def test_get_project_detail_404_for_other_orgs_project(client, fake_client):
    project, _job = _seed_project_with_job(fake_client, org_id="other-org")

    response = client.get(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 404


def test_reingest_missing_auth_returns_401():
    app = create_app()
    client = TestClient(app)

    response = client.post("/api/agent1/projects/does-not-exist/reingest")

    assert response.status_code == 401


def test_reingest_404_for_unknown_project(client):
    response = client.post("/api/agent1/projects/does-not-exist/reingest")

    assert response.status_code == 404


def test_reingest_404_for_other_orgs_project(client, fake_client):
    project, _job = _seed_project_with_job(fake_client, org_id="other-org")

    response = client.post(f"/api/agent1/projects/{project['id']}/reingest")

    assert response.status_code == 404


def test_reingest_github_project_creates_new_job_against_same_project(client, fake_client, enqueued):
    project, first_job = _seed_project_with_job(fake_client, status="done")

    response = client.post(f"/api/agent1/projects/{project['id']}/reingest")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"] != first_job["id"]

    jobs = fake_client.table("ingestion_jobs").rows
    assert len(jobs) == 2
    new_job = next(j for j in jobs if j["id"] == body["job_id"])
    assert new_job["project_id"] == project["id"]
    assert new_job["source_type"] == "github"
    assert new_job["source_ref"] == project["source_ref"]
    assert new_job["status"] == "queued"

    # Still only one project row — reingest never creates a second project.
    assert len(fake_client.table("projects").rows) == 1
    assert enqueued == [{"job_id": body["job_id"], "org_id": "org-123"}]


def test_reingest_github_project_stores_new_access_token(client, fake_client, enqueued, monkeypatch):
    project, _job = _seed_project_with_job(fake_client, status="done")

    stored = []

    async def fake_store(job_id, access_token):
        stored.append({"job_id": job_id, "access_token": access_token})
        return f"vault-ref-for-{job_id}"

    monkeypatch.setattr(jobs_module, "store_github_token", fake_store)

    response = client.post(
        f"/api/agent1/projects/{project['id']}/reingest",
        json={"access_token": "ghp_sekret"},
    )

    assert response.status_code == 202
    body = response.json()

    new_job = next(j for j in fake_client.table("ingestion_jobs").rows if j["id"] == body["job_id"])
    assert new_job["github_token_ref"] == f"vault-ref-for-{body['job_id']}"
    assert stored == [{"job_id": body["job_id"], "access_token": "ghp_sekret"}]


def test_reingest_upload_project_requires_new_file(client, fake_client):
    project = insert_scoped(
        fake_client,
        "projects",
        "org-123",
        {"source_type": "upload", "source_ref": "org-123/old-job/repo.zip"},
    ).data[0]

    response = client.post(f"/api/agent1/projects/{project['id']}/reingest")

    assert response.status_code == 400


def test_reingest_upload_project_creates_new_job_with_new_archive(client, fake_client, enqueued):
    project = insert_scoped(
        fake_client,
        "projects",
        "org-123",
        {"source_type": "upload", "source_ref": "org-123/old-job/repo.zip"},
    ).data[0]

    response = client.post(
        f"/api/agent1/projects/{project['id']}/reingest",
        files={"file": ("repo.zip", b"fake zip contents", "application/zip")},
    )

    assert response.status_code == 202
    body = response.json()

    jobs = fake_client.table("ingestion_jobs").rows
    [new_job] = jobs
    assert new_job["project_id"] == project["id"]
    assert new_job["source_type"] == "upload"
    assert new_job["source_ref"] != project["source_ref"]
    assert enqueued == [{"job_id": body["job_id"], "org_id": "org-123"}]
