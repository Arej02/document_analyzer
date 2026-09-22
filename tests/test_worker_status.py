from document_analyzer.supabase_client import insert_scoped
from document_analyzer.worker.status import mark_job_failed, mark_job_running, mark_progress_stage

from fakes import FakeClient


def test_mark_job_running_updates_status_and_progress_stage():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "queued"},
    ).data[0]

    mark_job_running(client, job["id"], "org-123")

    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "running"
    assert row["progress_stage"] == "extracting"


def test_mark_job_running_does_not_touch_another_orgs_job():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-A",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "queued"},
    ).data[0]

    mark_job_running(client, job["id"], "org-B")

    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "queued"
    assert row.get("progress_stage") is None


def test_mark_progress_stage_updates_only_progress_stage():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running"},
    ).data[0]

    mark_progress_stage(client, job["id"], "org-123", "summarizing")

    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "running"
    assert row["progress_stage"] == "summarizing"


def test_mark_job_failed_sets_status_and_error():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-123",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running"},
    ).data[0]

    mark_job_failed(client, job["id"], "org-123", "could not clone repo")

    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "failed"
    assert row["error"] == "could not clone repo"


def test_mark_job_failed_does_not_touch_another_orgs_job():
    client = FakeClient(org_id="org-123")
    job = insert_scoped(
        client,
        "ingestion_jobs",
        "org-A",
        {"project_id": "proj-1", "source_type": "github", "source_ref": "https://github.com/org/repo", "status": "running"},
    ).data[0]

    mark_job_failed(client, job["id"], "org-B", "boom")

    [row] = client.table("ingestion_jobs").rows
    assert row["status"] == "running"
    assert row.get("error") is None
