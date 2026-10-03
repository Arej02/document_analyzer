import pytest
from fastapi.testclient import TestClient

from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer import jobs as jobs_module

from fakes import FakeClient


@pytest.fixture
def fake_client():
    return FakeClient(org_id="org-123")


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    async def fake_enqueue(job_id, org_id):
        calls.append({"job_id": job_id, "org_id": org_id})

    monkeypatch.setattr(jobs_module, "enqueue_ingestion_job", fake_enqueue)
    return calls


@pytest.fixture
def stored_tokens(monkeypatch):
    calls = []

    async def fake_store(job_id, access_token):
        calls.append({"job_id": job_id, "access_token": access_token})
        return f"vault-ref-for-{job_id}"

    monkeypatch.setattr(jobs_module, "store_github_token", fake_store)
    return calls


@pytest.fixture(autouse=True)
def service_client(fake_client, monkeypatch):
    """The queue position is computed with the service-role client; point it at the fake."""
    monkeypatch.setattr(jobs_module, "get_service_role_client", lambda: fake_client)
    return fake_client


@pytest.fixture
def client(fake_client):
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app)


def test_create_job_missing_auth_returns_401():
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    )

    assert response.status_code == 401


def test_create_job_github_creates_project_and_job(client, fake_client, enqueued):
    response = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"]

    [project] = fake_client.table("projects").rows
    assert project["org_id"] == "org-123"
    assert project["source_type"] == "github"
    assert project["source_ref"] == "https://github.com/org/repo"

    [job] = fake_client.table("ingestion_jobs").rows
    assert job["project_id"] == project["id"]
    assert job["status"] == "queued"
    assert job["source_type"] == "github"

    assert enqueued == [{"job_id": body["job_id"], "org_id": "org-123"}]


def test_create_job_github_reuses_existing_project(client, fake_client, enqueued):
    payload = {"source_type": "github", "repo_url": "https://github.com/org/repo"}

    first = client.post("/api/agent1/jobs", json=payload).json()
    second = client.post("/api/agent1/jobs", json=payload).json()

    assert first["job_id"] != second["job_id"]
    assert len(fake_client.table("projects").rows) == 1
    assert len(fake_client.table("ingestion_jobs").rows) == 2


def test_create_job_github_rejects_missing_repo_url(client):
    response = client.post("/api/agent1/jobs", json={"source_type": "github"})

    assert response.status_code == 422


def test_create_job_github_private_repo_stores_token_in_vault(client, fake_client, enqueued, stored_tokens):
    response = client.post(
        "/api/agent1/jobs",
        json={
            "source_type": "github",
            "repo_url": "https://github.com/org/private-repo",
            "access_token": "ghp_sekret",
        },
    )

    assert response.status_code == 202
    body = response.json()

    [job] = fake_client.table("ingestion_jobs").rows
    assert job["id"] == body["job_id"]
    assert job["github_token_ref"] == f"vault-ref-for-{body['job_id']}"

    assert stored_tokens == [{"job_id": body["job_id"], "access_token": "ghp_sekret"}]
    # The raw token itself is never written onto the job row.
    assert "ghp_sekret" not in job.values()


def test_create_job_github_public_repo_skips_vault(client, fake_client, enqueued, stored_tokens):
    response = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    )

    assert response.status_code == 202
    [job] = fake_client.table("ingestion_jobs").rows
    assert job["github_token_ref"] is None
    assert stored_tokens == []


def test_create_job_upload_stores_file_and_creates_job(client, fake_client, enqueued):
    response = client.post(
        "/api/agent1/jobs",
        files={"file": ("repo.zip", b"fake-zip-bytes", "application/zip")},
    )

    assert response.status_code == 202
    body = response.json()

    [job] = fake_client.table("ingestion_jobs").rows
    assert job["source_type"] == "upload"
    assert job["id"] == body["job_id"]

    bucket = fake_client.storage.from_("agent1-uploads")
    [(path, contents, _opts)] = bucket.uploads
    assert path == job["source_ref"]
    assert path.startswith(f"org-123/{body['job_id']}/")
    assert contents == b"fake-zip-bytes"

    assert enqueued == [{"job_id": body["job_id"], "org_id": "org-123"}]


def test_create_job_upload_rejects_non_archive_filename(client):
    response = client.post(
        "/api/agent1/jobs",
        files={"file": ("readme.txt", b"not-an-archive", "text/plain")},
    )

    assert response.status_code == 400


def test_create_job_rejects_unsupported_content_type(client):
    response = client.post(
        "/api/agent1/jobs",
        content=b"plain text",
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 415


def test_create_job_upload_rejects_oversized_file(client, fake_client, monkeypatch):
    from document_analyzer import storage as storage_module

    monkeypatch.setattr(storage_module, "MAX_UPLOAD_SIZE_BYTES", 10)

    response = client.post(
        "/api/agent1/jobs",
        files={"file": ("repo.zip", b"0123456789ABCDEF", "application/zip")},
    )

    assert response.status_code == 413
    assert fake_client.table("ingestion_jobs").rows == []
    assert fake_client.storage.from_("agent1-uploads")._objects == {}


def test_get_job_returns_status_progress_and_error(client, fake_client, enqueued):
    created = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    ).json()
    job_id = created["job_id"]
    fake_client.table("ingestion_jobs").rows[0].update(
        {"status": "running", "progress_stage": "extracting", "error": None}
    )

    response = client.get(f"/api/agent1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id,
        "project_id": fake_client.table("ingestion_jobs").rows[0]["project_id"],
        "queue_position": None,
        "status": "running",
        "progress_stage": "extracting",
        "error": None,
        "changed_file_count": None,
        "unchanged_file_count": None,
        "removed_file_count": None,
        "discovery_stats": None,
        "source_commit_sha": None,
    }


def test_get_job_returns_source_commit_sha_once_recorded(client, fake_client, enqueued):
    created = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    ).json()
    sha = "0123456789abcdef0123456789abcdef01234567"
    fake_client.table("ingestion_jobs").rows[0]["source_commit_sha"] = sha

    body = client.get(f"/api/agent1/jobs/{created['job_id']}").json()

    assert body["source_commit_sha"] == sha


def test_get_job_returns_its_project_id(client, fake_client, enqueued):
    created = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    ).json()

    body = client.get(f"/api/agent1/jobs/{created['job_id']}").json()

    assert body["project_id"]
    assert body["project_id"] == fake_client.table("projects").rows[0]["id"]


def test_get_job_returns_diff_counts_once_the_worker_has_set_them(client, fake_client, enqueued):
    created = client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    ).json()
    job_id = created["job_id"]
    fake_client.table("ingestion_jobs").rows[0].update(
        {"changed_file_count": 3, "unchanged_file_count": 12, "removed_file_count": 1}
    )

    response = client.get(f"/api/agent1/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["changed_file_count"] == 3
    assert body["unchanged_file_count"] == 12
    assert body["removed_file_count"] == 1


def test_get_job_missing_returns_404(client):
    response = client.get("/api/agent1/jobs/does-not-exist")

    assert response.status_code == 404


def test_get_job_does_not_leak_another_orgs_job(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    other_org_job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        "org-other",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "queued"},
    ).data[0]

    response = client.get(f"/api/agent1/jobs/{other_org_job['id']}")

    assert response.status_code == 404


def test_get_job_missing_auth_returns_401():
    app = create_app()
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/agent1/jobs/some-id")

    assert response.status_code == 401


def _done_job(fake_client):
    from document_analyzer.supabase_client import insert_scoped

    return insert_scoped(
        fake_client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "done"},
    ).data[0]


def test_get_job_summary_returns_repo_summary_risk_level_counts_and_unverified_count(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _done_job(fake_client)
    insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "a", "file_path": "a.php", "raw_facts": {}})
    insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "b", "file_path": "a.php", "raw_facts": {}})
    insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "class", "name": "C", "file_path": "b.php", "raw_facts": {}})
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "repo", "summary_text": "A checkout plugin.", "risk_level": "medium"})
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "entity", "summary_text": "x", "risk_level": "high", "grounding_status": "unverified"})
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "entity", "summary_text": "y", "risk_level": "low", "grounding_status": "verified"})

    response = client.get(f"/api/agent1/jobs/{job['id']}/summary")

    assert response.status_code == 200
    assert response.json() == {
        "repo_summary": "A checkout plugin.",
        "risk_level": "medium",
        "entity_counts": {"function": 2, "class": 1},
        "unverified_count": 1,
    }


def test_get_job_summary_returns_null_repo_fields_when_no_repo_summary_persisted(client, fake_client):
    job = _done_job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/summary")

    assert response.status_code == 200
    assert response.json() == {
        "repo_summary": None,
        "risk_level": None,
        "entity_counts": {},
        "unverified_count": 0,
    }


def test_get_job_summary_not_done_yet_returns_409(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running"},
    ).data[0]

    response = client.get(f"/api/agent1/jobs/{job['id']}/summary")

    assert response.status_code == 409


def test_get_job_summary_missing_job_returns_404(client):
    response = client.get("/api/agent1/jobs/does-not-exist/summary")

    assert response.status_code == 404


def test_get_job_summary_does_not_leak_another_orgs_job(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    other_org_job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        "org-other",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "done"},
    ).data[0]

    response = client.get(f"/api/agent1/jobs/{other_org_job['id']}/summary")

    assert response.status_code == 404


def test_get_job_summary_missing_auth_returns_401():
    app = create_app()
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/agent1/jobs/some-id/summary")

    assert response.status_code == 401


def _job(fake_client, org_id="org-123", status="running"):
    from document_analyzer.supabase_client import insert_scoped

    return insert_scoped(
        fake_client,
        "ingestion_jobs",
        org_id,
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": status},
    ).data[0]


def test_get_job_entities_returns_paginated_joined_rows(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _job(fake_client)
    entity = insert_scoped(
        fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "process_payment", "file_path": "a.php", "raw_facts": {}}
    ).data[0]
    insert_scoped(
        fake_client,
        "knowledge_summaries",
        "org-123",
        {"job_id": job["id"], "level": "entity", "entity_id": entity["id"], "summary_text": "Charges the customer.", "risk_level": "high", "grounding_status": "verified"},
    )

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities")

    assert response.status_code == 200
    assert response.json() == {
        "entities": [
            {
                "id": entity["id"],
                "entity_type": "function",
                "name": "process_payment",
                "file_path": "a.php",
                "summary_text": "Charges the customer.",
                "risk_level": "high",
                "grounding_status": "verified",
            }
        ],
        "total_count": 1,
        "limit": 50,
        "offset": 0,
    }


def test_get_job_entities_does_not_require_job_to_be_done(client, fake_client):
    job = _job(fake_client, status="running")

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities")

    assert response.status_code == 200
    assert response.json()["entities"] == []


def test_get_job_entities_filters_by_entity_type_and_risk_level(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _job(fake_client)
    fn = insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "f", "file_path": "a.php", "raw_facts": {}}).data[0]
    cls = insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "class", "name": "C", "file_path": "a.php", "raw_facts": {}}).data[0]
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "entity", "entity_id": fn["id"], "summary_text": "x", "risk_level": "low"})
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "entity", "entity_id": cls["id"], "summary_text": "y", "risk_level": "high"})

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities", params={"entity_type": "class"})
    assert [e["name"] for e in response.json()["entities"]] == ["C"]

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities", params={"risk_level": "high"})
    assert [e["name"] for e in response.json()["entities"]] == ["C"]


def test_get_job_entities_respects_limit_and_offset(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _job(fake_client)
    for i in range(5):
        insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": f"fn_{i}", "file_path": "a.php", "raw_facts": {}})

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities", params={"limit": 2, "offset": 2})

    body = response.json()
    assert [e["name"] for e in body["entities"]] == ["fn_2", "fn_3"]
    assert body["total_count"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 2


def test_get_job_entities_rejects_limit_over_max(client, fake_client):
    job = _job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities", params={"limit": 500})

    assert response.status_code == 422


def test_get_job_entities_missing_job_returns_404(client):
    response = client.get("/api/agent1/jobs/does-not-exist/entities")

    assert response.status_code == 404


def test_get_job_entities_does_not_leak_another_orgs_job(client, fake_client):
    job = _job(fake_client, org_id="org-other")

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities")

    assert response.status_code == 404


def test_get_job_entities_missing_auth_returns_401():
    app = create_app()
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/agent1/jobs/some-id/entities")

    assert response.status_code == 401


def test_get_job_entity_detail_returns_raw_facts_summary_and_edges(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _job(fake_client)
    caller = insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "checkout", "file_path": "a.php", "raw_facts": {"calls": ["charge_card"]}}).data[0]
    callee = insert_scoped(fake_client, "extracted_entities", "org-123", {"job_id": job["id"], "entity_type": "function", "name": "charge_card", "file_path": "b.php", "raw_facts": {}}).data[0]
    insert_scoped(fake_client, "knowledge_summaries", "org-123", {"job_id": job["id"], "level": "entity", "entity_id": caller["id"], "summary_text": "Handles checkout.", "risk_level": "high", "grounding_status": "verified"})
    insert_scoped(fake_client, "entity_edges", "org-123", {"job_id": job["id"], "from_entity_id": caller["id"], "to_entity_id": callee["id"], "edge_type": "calls"})

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities/{caller['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == caller["id"]
    assert body["raw_facts"] == {"calls": ["charge_card"]}
    assert body["summary_text"] == "Handles checkout."
    assert body["risk_level"] == "high"
    assert body["grounding_status"] == "verified"
    assert body["edges"]["outgoing"] == [
        {"id": body["edges"]["outgoing"][0]["id"], "edge_type": "calls", "to_entity": {"id": callee["id"], "entity_type": "function", "name": "charge_card", "file_path": "b.php"}}
    ]
    assert body["edges"]["incoming"] == []


def test_get_job_entity_detail_missing_entity_returns_404(client, fake_client):
    job = _job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities/does-not-exist")

    assert response.status_code == 404


def test_get_job_entity_detail_missing_job_returns_404(client):
    response = client.get("/api/agent1/jobs/does-not-exist/entities/some-entity")

    assert response.status_code == 404


def test_get_job_entity_detail_does_not_leak_another_orgs_entity(client, fake_client):
    from document_analyzer.supabase_client import insert_scoped

    job = _job(fake_client, org_id="org-other")
    entity = insert_scoped(fake_client, "extracted_entities", "org-other", {"job_id": job["id"], "entity_type": "function", "name": "not_mine", "file_path": "a.php", "raw_facts": {}}).data[0]

    response = client.get(f"/api/agent1/jobs/{job['id']}/entities/{entity['id']}")

    assert response.status_code == 404


def test_get_job_entity_detail_missing_auth_returns_401():
    app = create_app()
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/agent1/jobs/some-id/entities/some-entity")

    assert response.status_code == 401


# --- GET /jobs/{job_id}/events — live workflow run view (design.md §2, §4) --------


def _event(fake_client, job_id, org_id="org-123", **overrides):
    from document_analyzer.supabase_client import insert_scoped

    row = {
        "workflow_kind": "agent1_ingestion",
        "workflow_id": job_id,
        "event_type": "activity",
        "stage": "fetch",
        "message": "did something",
        "data": {},
        **overrides,
    }
    return insert_scoped(fake_client, "workflow_events", org_id, row).data[0]


def test_get_job_events_returns_events_oldest_first_with_a_cursor(client, fake_client):
    job = _job(fake_client)
    e1 = _event(fake_client, job["id"], message="first")
    e2 = _event(fake_client, job["id"], message="second")

    response = client.get(f"/api/agent1/jobs/{job['id']}/events")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job["id"]
    assert body["status"] == "running"
    assert [e["id"] for e in body["events"]] == [e1["id"], e2["id"]]
    assert [e["message"] for e in body["events"]] == ["first", "second"]
    assert body["next_cursor"] == e2["id"]
    assert body["server_time"]


def test_get_job_events_returns_the_full_event_shape(client, fake_client):
    job = _job(fake_client)
    event = _event(
        fake_client,
        job["id"],
        event_type="output.created",
        stage="snapshot",
        message=None,
        data={"output_id": "source_snapshot", "kind": "snapshot", "label": "Source snapshot", "status": "done"},
    )

    response = client.get(f"/api/agent1/jobs/{job['id']}/events")

    [returned] = response.json()["events"]
    assert returned == {
        "id": event["id"],
        "event_type": "output.created",
        "stage": "snapshot",
        "message": None,
        "data": {"output_id": "source_snapshot", "kind": "snapshot", "label": "Source snapshot", "status": "done"},
        "created_at": event["created_at"],
    }


def test_get_job_events_pages_by_cursor(client, fake_client):
    job = _job(fake_client)
    events = [_event(fake_client, job["id"], message=f"event {i}") for i in range(3)]

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"after": events[0]["id"]})

    body = response.json()
    assert [e["id"] for e in body["events"]] == [events[1]["id"], events[2]["id"]]
    assert body["next_cursor"] == events[2]["id"]


def test_get_job_events_next_cursor_is_unchanged_when_there_is_nothing_new(client, fake_client):
    """A client must always be able to pass next_cursor straight back as the next
    call's `after`, even on an empty page — design.md §4."""
    job = _job(fake_client)
    event = _event(fake_client, job["id"])

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"after": event["id"]})

    body = response.json()
    assert body["events"] == []
    assert body["next_cursor"] == event["id"]


def test_get_job_events_with_no_events_yet_returns_the_requested_after_as_cursor(client, fake_client):
    job = _job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"after": 7})

    body = response.json()
    assert body["events"] == []
    assert body["next_cursor"] == 7


def test_get_job_events_respects_the_limit_cap(client, fake_client):
    job = _job(fake_client)
    events = [_event(fake_client, job["id"], message=f"event {i}") for i in range(5)]

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"limit": 2})

    body = response.json()
    assert [e["id"] for e in body["events"]] == [events[0]["id"], events[1]["id"]]
    assert body["next_cursor"] == events[1]["id"]


def test_get_job_events_rejects_limit_over_the_max(client, fake_client):
    job = _job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"limit": 501})

    assert response.status_code == 422


def test_get_job_events_rejects_a_negative_after(client, fake_client):
    job = _job(fake_client)

    response = client.get(f"/api/agent1/jobs/{job['id']}/events", params={"after": -1})

    assert response.status_code == 422


def test_get_job_events_does_not_require_job_to_be_done(client, fake_client):
    job = _job(fake_client, status="queued")

    response = client.get(f"/api/agent1/jobs/{job['id']}/events")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_get_job_events_missing_job_returns_404(client):
    response = client.get("/api/agent1/jobs/does-not-exist/events")

    assert response.status_code == 404


def test_get_job_events_does_not_leak_another_orgs_job(client, fake_client):
    job = _job(fake_client, org_id="org-other")

    response = client.get(f"/api/agent1/jobs/{job['id']}/events")

    assert response.status_code == 404


def test_get_job_events_does_not_leak_another_orgs_events(client, fake_client):
    """Defense-in-depth on the events query itself (filtered by org_id, not just the
    job lookup) — same pattern as every other worker/org-scoped read in this codebase,
    even though workflow_events.workflow_id can't actually collide across orgs in
    practice (it's always this org's own job id)."""
    job = _job(fake_client)
    _event(fake_client, job["id"], org_id="org-other")

    response = client.get(f"/api/agent1/jobs/{job['id']}/events")

    assert response.json()["events"] == []


def test_get_job_events_missing_auth_returns_401():
    app = create_app()
    unauthenticated = TestClient(app)

    response = unauthenticated.get("/api/agent1/jobs/some-id/events")

    assert response.status_code == 401


def _create_job(client):
    return client.post(
        "/api/agent1/jobs",
        json={"source_type": "github", "repo_url": "https://github.com/org/repo"},
    ).json()["job_id"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_cancel_job_stops_a_queued_or_running_job(client, fake_client, enqueued, service_client, status):
    job_id = _create_job(client)
    fake_client.table("ingestion_jobs").rows[0]["status"] = status

    response = client.post(f"/api/agent1/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "status": "cancelled"}
    assert fake_client.table("ingestion_jobs").rows[0]["status"] == "cancelled"
    assert client.get(f"/api/agent1/jobs/{job_id}").json()["status"] == "cancelled"


def test_cancel_job_is_idempotent(client, fake_client, enqueued, service_client):
    job_id = _create_job(client)
    client.post(f"/api/agent1/jobs/{job_id}/cancel")

    again = client.post(f"/api/agent1/jobs/{job_id}/cancel")

    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_cancel_job_refuses_a_finished_job_and_leaves_it_alone(client, fake_client, enqueued, service_client, status):
    job_id = _create_job(client)
    fake_client.table("ingestion_jobs").rows[0]["status"] = status

    response = client.post(f"/api/agent1/jobs/{job_id}/cancel")

    assert response.status_code == 409
    assert fake_client.table("ingestion_jobs").rows[0]["status"] == status


def test_cancel_job_missing_returns_404(client, service_client):
    assert client.post("/api/agent1/jobs/does-not-exist/cancel").status_code == 404


def test_cancel_job_other_orgs_job_returns_404_and_is_untouched(client, fake_client, enqueued, service_client):
    job_id = _create_job(client)
    fake_client.table("ingestion_jobs").rows[0]["org_id"] = "other-org"

    assert client.post(f"/api/agent1/jobs/{job_id}/cancel").status_code == 404
    assert fake_client.table("ingestion_jobs").rows[0]["status"] == "queued"


def test_cancel_job_missing_auth_returns_401():
    response = TestClient(create_app()).post("/api/agent1/jobs/abc/cancel")
    assert response.status_code == 401


def _queued_job(client):
    return _create_job(client)


def test_a_queued_job_reports_how_many_jobs_are_ahead(client, fake_client, enqueued):
    first = _queued_job(client)
    second = _queued_job(client)
    third = _queued_job(client)

    assert client.get(f"/api/agent1/jobs/{first}").json()["queue_position"] == 0
    assert client.get(f"/api/agent1/jobs/{second}").json()["queue_position"] == 1
    assert client.get(f"/api/agent1/jobs/{third}").json()["queue_position"] == 2


def test_running_jobs_count_as_ahead(client, fake_client, enqueued):
    running = _queued_job(client)
    waiting = _queued_job(client)
    fake_client.table("ingestion_jobs").rows[0]["status"] = "running"

    assert client.get(f"/api/agent1/jobs/{running}").json()["queue_position"] is None
    assert client.get(f"/api/agent1/jobs/{waiting}").json()["queue_position"] == 1


def test_the_position_counts_other_orgs_jobs_but_reveals_nothing_about_them(client, fake_client, enqueued):
    mine = _queued_job(client)
    fake_client.table("ingestion_jobs").insert(
        {"id": "other-job", "org_id": "other-org", "source_type": "github", "source_ref": "x", "status": "queued", "created_at": 0}
    ).execute()

    body = client.get(f"/api/agent1/jobs/{mine}").json()

    assert body["queue_position"] == 1
    assert "other-job" not in str(body)


def test_queue_position_is_null_unless_the_job_is_queued(client, fake_client, enqueued):
    job_id = _queued_job(client)
    for status in ("done", "failed", "cancelled"):
        fake_client.table("ingestion_jobs").rows[0]["status"] = status
        assert client.get(f"/api/agent1/jobs/{job_id}").json()["queue_position"] is None


def test_a_failure_computing_the_position_does_not_fail_the_poll(client, fake_client, enqueued, monkeypatch):
    job_id = _queued_job(client)

    def boom():
        raise RuntimeError("no service key")

    monkeypatch.setattr(jobs_module, "get_service_role_client", boom)

    response = client.get(f"/api/agent1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["queue_position"] is None
