"""DELETE /api/agent1/projects/{project_id} — design.md §4."""

import pytest
from fastapi.testclient import TestClient

from document_analyzer import projects_router as projects_router_module
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient

AGENT1_TABLES = (
    "ingestion_jobs",
    "extracted_entities",
    "entity_edges",
    "knowledge_summaries",
    "filtered_files",
    "secret_findings",
    "source_chunks",
    "source_chunk_entities",
)
AGENT2_TABLES = (
    "planning_jobs",
    "clusters",
    "migration_plans",
    "migration_plan_items",
    "phase_approvals",
    "plan_overrides",
)
AGENT3_TABLES = ("execution_jobs", "unit_executions")


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient(org_id="org-123")
    client.cascades = {"planning_jobs": [("clusters", "planning_job_id")]}
    monkeypatch.setattr(projects_router_module, "get_service_role_client", lambda: client)
    return client


@pytest.fixture
def api(fake_client):
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=fake_client.org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app)


def _insert(client, table, org_id, row):
    return insert_scoped(client, table, org_id, row).data[0]


def _seed_project(client, org_id="org-123", source_type="github", source_ref="https://github.com/org/repo", status="done"):
    """A project with one finished run and one finished plan, touching every table."""
    project = _insert(client, "projects", org_id, {"source_type": source_type, "source_ref": source_ref})
    job = _insert(
        client,
        "ingestion_jobs",
        org_id,
        {"project_id": project["id"], "source_type": source_type, "source_ref": source_ref, "status": status},
    )
    for row in client.table("projects").rows:
        if row["id"] == project["id"]:
            row["latest_job_id"] = job["id"]

    a = _insert(client, "extracted_entities", org_id, {"job_id": job["id"], "name": "a", "content_hash": f"h-{project['id']}"})
    b = _insert(client, "extracted_entities", org_id, {"job_id": job["id"], "name": "b", "content_hash": "h-b"})
    _insert(client, "entity_edges", org_id, {"job_id": job["id"], "from_entity_id": a["id"], "to_entity_id": b["id"]})
    summary = _insert(client, "knowledge_summaries", org_id, {"job_id": job["id"], "entity_id": a["id"], "level": "entity"})
    _insert(client, "summary_cache", org_id, {"content_hash": f"h-{project['id']}", "summary_id": summary["id"]})
    _insert(client, "filtered_files", org_id, {"job_id": job["id"], "file_path": "vendor/x.php"})
    _insert(client, "secret_findings", org_id, {"job_id": job["id"], "entity_id": a["id"]})
    chunk = _insert(client, "source_chunks", org_id, {"job_id": job["id"], "file_path": "a.php", "content": "x"})
    _insert(client, "source_chunk_entities", org_id, {"job_id": job["id"], "chunk_id": chunk["id"], "entity_id": a["id"]})

    planning_job = _insert(
        client, "planning_jobs", org_id, {"project_id": project["id"], "source_job_id": job["id"], "status": "done"}
    )
    unit = _insert(client, "clusters", org_id, {"planning_job_id": planning_job["id"], "level": "unit"})
    plan = _insert(
        client,
        "migration_plans",
        org_id,
        {"project_id": project["id"], "planning_job_id": planning_job["id"], "source_job_id": job["id"]},
    )
    _insert(client, "migration_plan_items", org_id, {"plan_id": plan["id"], "cluster_id": unit["id"]})
    _insert(client, "phase_approvals", org_id, {"plan_id": plan["id"], "phase_cluster_id": unit["id"]})
    _insert(client, "plan_overrides", org_id, {"plan_id": plan["id"], "kind": "pin"})

    execution_job = _insert(
        client,
        "execution_jobs",
        org_id,
        {"project_id": project["id"], "plan_id": plan["id"], "source_job_id": job["id"], "status": "done"},
    )
    _insert(client, "unit_executions", org_id, {"execution_job_id": execution_job["id"], "cluster_id": unit["id"]})
    return project, job, planning_job


def _rows(client, table):
    return client.table(table).rows


def test_delete_removes_the_project_and_everything_derived_from_it(api, fake_client):
    project, _job, _pj = _seed_project(fake_client)

    response = api.delete(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 204
    assert _rows(fake_client, "projects") == []
    for table in AGENT1_TABLES + AGENT2_TABLES + AGENT3_TABLES + ("summary_cache",):
        assert _rows(fake_client, table) == [], table


def test_delete_leaves_other_projects_and_other_orgs_untouched(api, fake_client):
    target, _, _ = _seed_project(fake_client)
    other, _, _ = _seed_project(fake_client, source_ref="https://github.com/org/other")
    foreign, _, _ = _seed_project(fake_client, org_id="org-999", source_ref="https://github.com/org/repo")
    before = {t: len(_rows(fake_client, t)) for t in AGENT1_TABLES + AGENT2_TABLES + AGENT3_TABLES + ("summary_cache",)}

    assert api.delete(f"/api/agent1/projects/{target['id']}").status_code == 204

    assert {p["id"] for p in _rows(fake_client, "projects")} == {other["id"], foreign["id"]}
    for table, count in before.items():
        # Each seeded project contributes the same number of rows to every table.
        assert len(_rows(fake_client, table)) == count * 2 // 3, table


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_is_refused_while_an_ingestion_job_is_active(api, fake_client, status):
    project, _, _ = _seed_project(fake_client, status=status)

    response = api.delete(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 409
    assert len(_rows(fake_client, "projects")) == 1
    assert len(_rows(fake_client, "extracted_entities")) == 2


def test_delete_is_refused_while_a_planning_job_is_active(api, fake_client):
    project, _, planning_job = _seed_project(fake_client)
    for row in _rows(fake_client, "planning_jobs"):
        if row["id"] == planning_job["id"]:
            row["status"] = "running"

    response = api.delete(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 409
    assert len(_rows(fake_client, "migration_plans")) == 1


def test_delete_of_another_orgs_project_is_404_and_deletes_nothing(api, fake_client):
    foreign, _, _ = _seed_project(fake_client, org_id="org-999")

    response = api.delete(f"/api/agent1/projects/{foreign['id']}")

    assert response.status_code == 404
    assert len(_rows(fake_client, "projects")) == 1
    assert len(_rows(fake_client, "extracted_entities")) == 2


def test_delete_of_a_missing_project_is_404(api):
    assert api.delete("/api/agent1/projects/does-not-exist").status_code == 404


def test_delete_removes_uploaded_archives(api, fake_client):
    bucket = fake_client.storage.from_("agent1-uploads")
    bucket.upload("org-123/job-1/repo.zip", b"zip")
    bucket.upload("org-123/job-2/keep.zip", b"zip")
    project, _, _ = _seed_project(fake_client, source_type="upload", source_ref="org-123/job-1/repo.zip")

    assert api.delete(f"/api/agent1/projects/{project['id']}").status_code == 204

    assert set(bucket._objects) == {"org-123/job-2/keep.zip"}


def test_delete_removes_source_snapshots(api, fake_client):
    """Agent 3 prerequisite (design_agent3.md §13) — a job's source_snapshot_path
    tarball (worker/snapshot.py) is deleted alongside its other Agent 1 rows,
    for both github- and upload-sourced projects (unlike agent1-uploads archives,
    a snapshot exists regardless of source_type)."""
    bucket = fake_client.storage.from_("agent1-snapshots")
    bucket.upload("org-123/job-1/source.tar.gz", b"tar")
    bucket.upload("org-123/job-2/source.tar.gz", b"tar")
    project, job, _ = _seed_project(fake_client)
    for row in _rows(fake_client, "ingestion_jobs"):
        if row["id"] == job["id"]:
            row["source_snapshot_path"] = "org-123/job-1/source.tar.gz"

    assert api.delete(f"/api/agent1/projects/{project['id']}").status_code == 204

    assert set(bucket._objects) == {"org-123/job-2/source.tar.gz"}


def test_delete_tolerates_a_job_with_no_source_snapshot_path(api, fake_client):
    """A job predating this column (or one still mid-pipeline) has no snapshot to
    remove — deletion must not fail just because there's nothing to clean up."""
    project, _, _ = _seed_project(fake_client)

    assert api.delete(f"/api/agent1/projects/{project['id']}").status_code == 204


# --- Agent 3 (design_agent3.md §13) -------------------------------------------


def _execution_job_of(client, project):
    return next(row for row in _rows(client, "execution_jobs") if row["project_id"] == project["id"])


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_is_refused_while_a_code_generation_job_is_active(api, fake_client, status):
    project, _, _ = _seed_project(fake_client)
    _execution_job_of(fake_client, project)["status"] = status

    response = api.delete(f"/api/agent1/projects/{project['id']}")

    assert response.status_code == 409
    assert "code generation" in response.json()["detail"]
    assert len(_rows(fake_client, "execution_jobs")) == 1
    assert len(_rows(fake_client, "unit_executions")) == 1
    assert len(_rows(fake_client, "migration_plans")) == 1


def test_delete_removes_generated_code_recursively(api, fake_client):
    """agent3-output holds every attempt's files under nested folders
    ({org_id}/{execution_job_id}/units/{cluster_id}/attempt_n/...), plus the zip."""
    project, _, _ = _seed_project(fake_client)
    other, _, _ = _seed_project(fake_client, source_ref="https://github.com/org/other")
    job_id = _execution_job_of(fake_client, project)["id"]
    other_job_id = _execution_job_of(fake_client, other)["id"]
    bucket = fake_client.storage.from_("agent3-output")
    mine = [
        f"org-123/{job_id}/units/u1/attempt_1/code/app/units/x/main.py",
        f"org-123/{job_id}/units/u1/attempt_1/log.txt",
        f"org-123/{job_id}/units/u1/attempt_2/junit.xml",
        f"org-123/{job_id}/units/u2/attempt_1/log.txt",
        f"org-123/{job_id}/project.zip",
    ]
    kept = f"org-123/{other_job_id}/project.zip"
    for path in mine + [kept]:
        bucket.upload(path, b"x")

    assert api.delete(f"/api/agent1/projects/{project['id']}").status_code == 204

    assert set(bucket._objects) == {kept}


def test_delete_tolerates_a_storage_failure_on_generated_code(api, fake_client, monkeypatch):
    project, _, _ = _seed_project(fake_client)
    bucket = fake_client.storage.from_("agent3-output")

    def broken_list(*args, **kwargs):
        raise Exception("storage unavailable")

    monkeypatch.setattr(bucket, "list", broken_list)

    assert api.delete(f"/api/agent1/projects/{project['id']}").status_code == 204
    assert _rows(fake_client, "execution_jobs") == []


def test_list_objects_pages_through_large_folders(fake_client, monkeypatch):
    from document_analyzer import projects

    monkeypatch.setattr(projects, "_PAGE_SIZE", 2)
    bucket = fake_client.storage.from_("agent3-output")
    for n in range(5):
        bucket.upload(f"org-123/job/units/u{n}/log.txt", b"x")

    assert sorted(projects._list_objects(bucket, "org-123/job")) == [f"org-123/job/units/u{n}/log.txt" for n in range(5)]
