from document_analyzer.supabase_client import insert_scoped
from document_analyzer.worker.job_completion import job_ready_for_completion, mark_job_done_if_ready

from fakes import FakeClient


def _entity(client, org_id, job_id):
    return insert_scoped(
        client,
        "extracted_entities",
        org_id,
        {"job_id": job_id, "entity_type": "function", "name": "f", "file_path": "a.php", "raw_facts": {}},
    ).data[0]


def _entity_summary(client, org_id, job_id, entity_id, grounding_status):
    return insert_scoped(
        client,
        "knowledge_summaries",
        org_id,
        {
            "job_id": job_id,
            "level": "entity",
            "entity_id": entity_id,
            "summary_text": "x",
            "risk_level": "low",
            "grounding_status": grounding_status,
        },
    ).data[0]


def test_job_with_no_entities_is_ready_for_completion():
    client = FakeClient(org_id="org-123")

    assert job_ready_for_completion(client, "job-1", "org-123") is True


def test_job_not_ready_when_an_entity_has_no_summary_yet():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-123", "job-1")

    assert job_ready_for_completion(client, "job-1", "org-123") is False
    assert entity  # entity exists but no summary row was created for it


def test_job_ready_once_every_entity_has_a_summary_regardless_of_grounding_status():
    client = FakeClient(org_id="org-123")
    e1 = _entity(client, "org-123", "job-1")
    e2 = _entity(client, "org-123", "job-1")
    _entity_summary(client, "org-123", "job-1", e1["id"], "verified")
    _entity_summary(client, "org-123", "job-1", e2["id"], "unverified")

    assert job_ready_for_completion(client, "job-1", "org-123") is True


def test_job_not_ready_when_only_some_entities_have_summaries():
    client = FakeClient(org_id="org-123")
    e1 = _entity(client, "org-123", "job-1")
    _entity(client, "org-123", "job-1")
    _entity_summary(client, "org-123", "job-1", e1["id"], "verified")

    assert job_ready_for_completion(client, "job-1", "org-123") is False


def test_mark_job_done_if_ready_transitions_status_when_ready():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running", "progress_stage": "grounding"},
    ).data[0]
    e1 = _entity(client, "org-123", job["id"])
    _entity_summary(client, "org-123", job["id"], e1["id"], "unverified")

    transitioned = mark_job_done_if_ready(client, job["id"], "org-123")

    assert transitioned is True
    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "done"
    assert row["progress_stage"] is None


def test_mark_job_done_if_ready_does_nothing_when_not_ready():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running", "progress_stage": "grounding"},
    ).data[0]
    _entity(client, "org-123", job["id"])

    transitioned = mark_job_done_if_ready(client, job["id"], "org-123")

    assert transitioned is False
    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "running"


def test_mark_job_done_if_ready_does_not_touch_another_orgs_job():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-A",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running", "progress_stage": "grounding"},
    ).data[0]

    transitioned = mark_job_done_if_ready(client, job["id"], "org-B")

    assert transitioned is False
    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "running"


def test_mark_job_done_if_ready_updates_projects_latest_job_id():
    client = FakeClient(org_id="org-123")
    project = insert_scoped(
        client, "projects", "org-123", {"source_type": "github", "source_ref": "https://github.com/org/repo"}
    ).data[0]
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": project["id"], "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running", "progress_stage": "grounding"},
    ).data[0]
    e1 = _entity(client, "org-123", job["id"])
    _entity_summary(client, "org-123", job["id"], e1["id"], "verified")

    transitioned = mark_job_done_if_ready(client, job["id"], "org-123")

    assert transitioned is True
    [project_row] = client.table("projects").rows
    assert project_row["latest_job_id"] == job["id"]


def test_mark_job_done_if_ready_does_not_update_project_when_not_ready():
    client = FakeClient(org_id="org-123")
    project = insert_scoped(
        client, "projects", "org-123", {"source_type": "github", "source_ref": "https://github.com/org/repo"}
    ).data[0]
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": project["id"], "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running", "progress_stage": "grounding"},
    ).data[0]
    _entity(client, "org-123", job["id"])

    transitioned = mark_job_done_if_ready(client, job["id"], "org-123")

    assert transitioned is False
    [project_row] = client.table("projects").rows
    assert project_row.get("latest_job_id") is None


def test_mark_job_done_if_ready_does_not_update_another_orgs_project():
    client = FakeClient(org_id="org-123")
    other_org_project = insert_scoped(
        client, "projects", "org-A", {"source_type": "github", "source_ref": "https://github.com/org/repo"}
    ).data[0]
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-B",
        {
            "project_id": other_org_project["id"],
            "source_type": "github",
            "source_ref": "https://github.com/org/repo",
            "status": "running",
            "progress_stage": "grounding",
        },
    ).data[0]

    transitioned = mark_job_done_if_ready(client, job["id"], "org-B")

    assert transitioned is True  # zero entities, trivially ready
    [project_row] = client.table("projects").rows
    assert project_row["org_id"] == "org-A"
    assert project_row.get("latest_job_id") is None  # the org-A project was never touched by org-B's job
