from document_analyzer.supabase_client import insert_many_scoped, insert_scoped
from document_analyzer.worker.diff import diff_against_previous_job, persist_diff_counts

from fakes import FakeClient

ORG = "org-123"
PROJECT = "project-1"


def _seed_job(client, job_id, status, project_id=PROJECT):
    insert_scoped(client, "ingestion_jobs", ORG, {"id": job_id, "project_id": project_id, "status": status})


def _seed_entities(client, job_id, rows):
    insert_many_scoped(
        client,
        "extracted_entities",
        ORG,
        [{"job_id": job_id, "entity_type": "function", "raw_facts": {}, **row} for row in rows],
    )


def test_first_ingestion_has_no_previous_job_everything_changed():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="running")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-1")

    assert diff.previous_job_id is None
    assert diff.changed_file_paths == {"app.php"}
    assert diff.unchanged_file_paths == set()
    assert diff.removed_file_paths == set()


def test_unchanged_file_stays_unchanged():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(client, "job-2", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.previous_job_id == "job-1"
    assert diff.unchanged_file_paths == {"app.php"}
    assert diff.changed_file_paths == set()
    assert diff.removed_file_paths == set()


def test_changed_content_hash_marks_file_changed():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(client, "job-2", [{"name": "foo", "file_path": "app.php", "content_hash": "h2"}])

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.changed_file_paths == {"app.php"}
    assert diff.unchanged_file_paths == set()


def test_new_file_counts_as_changed():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(
        client,
        "job-2",
        [
            {"name": "foo", "file_path": "app.php", "content_hash": "h1"},
            {"name": "bar", "file_path": "new.php", "content_hash": "h9"},
        ],
    )

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.unchanged_file_paths == {"app.php"}
    assert diff.changed_file_paths == {"new.php"}


def test_removed_file_no_longer_present_in_current_job():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "gone.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(client, "job-2", [])

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.removed_file_paths == {"gone.php"}
    assert diff.changed_file_paths == set()
    assert diff.unchanged_file_paths == set()


def test_file_with_one_unchanged_and_one_new_entity_counts_as_changed():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(client, "job-1", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(
        client,
        "job-2",
        [
            {"name": "foo", "file_path": "app.php", "content_hash": "h1"},
            {"name": "bar", "file_path": "app.php", "content_hash": "h2"},
        ],
    )

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.changed_file_paths == {"app.php"}
    assert diff.unchanged_file_paths == set()


def test_only_matches_a_done_job_for_the_same_project():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "failed-job", status="failed")
    _seed_entities(client, "failed-job", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "other-project-job", status="done", project_id="other-project")
    _seed_entities(client, "other-project-job", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])
    _seed_job(client, "job-2", status="running")
    _seed_entities(client, "job-2", [{"name": "foo", "file_path": "app.php", "content_hash": "h1"}])

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.previous_job_id is None
    assert diff.changed_file_paths == {"app.php"}


def test_diff_counts_expose_lengths():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-1", status="done")
    _seed_entities(
        client,
        "job-1",
        [
            {"name": "a", "file_path": "unchanged.php", "content_hash": "h1"},
            {"name": "b", "file_path": "gone.php", "content_hash": "h2"},
        ],
    )
    _seed_job(client, "job-2", status="running")
    _seed_entities(
        client,
        "job-2",
        [
            {"name": "a", "file_path": "unchanged.php", "content_hash": "h1"},
            {"name": "c", "file_path": "new.php", "content_hash": "h3"},
        ],
    )

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")

    assert diff.unchanged_file_count == 1
    assert diff.changed_file_count == 1
    assert diff.removed_file_count == 1


def test_persist_diff_counts_writes_onto_job_row_scoped_to_org():
    client = FakeClient(org_id=ORG)
    _seed_job(client, "job-2", status="running")
    _seed_job(client, "other-org-job", status="running")
    other_org_job = client.table("ingestion_jobs").rows[-1]
    other_org_job["org_id"] = "other-org"

    diff = diff_against_previous_job(client, ORG, PROJECT, current_job_id="job-2")
    persist_diff_counts(client, "job-2", ORG, diff)

    [job_row] = [r for r in client.table("ingestion_jobs").rows if r["id"] == "job-2"]
    assert job_row["changed_file_count"] == 0
    assert job_row["unchanged_file_count"] == 0
    assert job_row["removed_file_count"] == 0

    untouched = [r for r in client.table("ingestion_jobs").rows if r["id"] == "other-org-job"][0]
    assert "changed_file_count" not in untouched
