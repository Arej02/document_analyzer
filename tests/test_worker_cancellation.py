"""Cooperative cancellation of an Agent 1 job — design.md §4 `POST /jobs/{job_id}/cancel`."""

import pytest

from document_analyzer.worker import cancellation
from document_analyzer.worker import job_completion as job_completion_module
from document_analyzer.worker import run as run_module
from document_analyzer.worker.cancellation import JobCancelled, forget_job, raise_if_cancelled
from document_analyzer.worker.fetch import FetchResult
from document_analyzer.worker.status import mark_job_failed

from fakes import FakeClient
from test_worker_run import _patch_grounded_pipeline, _seed_entities, _seed_job, _workflow_events


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    for job_id in ("job-1", "job-2"):
        forget_job(job_id)


def _client_with_job(status="running"):
    client = FakeClient(org_id="org-1")
    _seed_job(client, "job-1", "org-1")
    client.table("ingestion_jobs").rows[-1]["status"] = status
    return client


def test_raise_if_cancelled_is_silent_for_a_live_job():
    raise_if_cancelled(_client_with_job("running"), "job-1", "org-1")


def test_raise_if_cancelled_raises_for_a_cancelled_job():
    with pytest.raises(JobCancelled):
        raise_if_cancelled(_client_with_job("cancelled"), "job-1", "org-1")


def test_the_check_is_throttled_but_remembers_a_cancel_once_seen():
    client = _client_with_job("running")
    clock = [0.0]
    raise_if_cancelled(client, "job-1", "org-1", now=lambda: clock[0])

    client.table("ingestion_jobs").rows[0]["status"] = "cancelled"
    clock[0] = 0.5  # inside the throttle window: no query, so the cancel isn't seen yet
    raise_if_cancelled(client, "job-1", "org-1", now=lambda: clock[0])

    clock[0] = 1.5
    with pytest.raises(JobCancelled):
        raise_if_cancelled(client, "job-1", "org-1", now=lambda: clock[0])

    # Remembered: still raises with no further read, even inside the window and after a status change.
    client.table("ingestion_jobs").rows[0]["status"] = "running"
    with pytest.raises(JobCancelled):
        raise_if_cancelled(client, "job-1", "org-1", now=lambda: clock[0])


def test_force_skips_the_throttle():
    client = _client_with_job("running")
    raise_if_cancelled(client, "job-1", "org-1", now=lambda: 0.0)
    client.table("ingestion_jobs").rows[0]["status"] = "cancelled"
    with pytest.raises(JobCancelled):
        raise_if_cancelled(client, "job-1", "org-1", force=True, now=lambda: 0.1)


def test_a_failed_status_read_is_not_a_cancel():
    class Broken:
        def table(self, name):
            raise RuntimeError("database unavailable")

    raise_if_cancelled(Broken(), "job-1", "org-1")


def test_cancellation_is_scoped_to_the_org():
    client = _client_with_job("cancelled")
    raise_if_cancelled(client, "job-1", "some-other-org")  # not this org's job: not seen


def test_a_cancelled_job_is_never_overwritten_by_failed_or_done():
    client = _client_with_job("cancelled")

    mark_job_failed(client, "job-1", "org-1", "boom")
    assert client.table("ingestion_jobs").rows[0]["status"] == "cancelled"

    assert job_completion_module.mark_job_done_if_ready(client, "job-1", "org-1") is False
    assert client.table("ingestion_jobs").rows[0]["status"] == "cancelled"


def test_process_job_skips_a_job_cancelled_while_queued(monkeypatch):
    client = _client_with_job("cancelled")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a cancelled job must not run")

    monkeypatch.setattr(run_module, "fetch_source", fail_if_called)

    run_module.process_job("job-1", "org-1")

    assert client.table("ingestion_jobs").rows[0]["status"] == "cancelled"
    assert _workflow_events(client) == []


def test_process_job_stops_at_the_next_stage_when_cancelled_mid_run(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1")

    def fetch_then_get_cancelled(client_, job, dest_dir):
        # The user presses Cancel while the fetch is running.
        client.table("ingestion_jobs").rows[0]["status"] = "cancelled"
        return FetchResult(repo_path=dest_dir, source_commit_sha=None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("no work may start after a cancel")

    monkeypatch.setattr(run_module, "fetch_source", fetch_then_get_cancelled)
    monkeypatch.setattr(run_module, "persist_source_commit_sha", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "_strip_boilerplate", fail_if_called)
    monkeypatch.setattr(run_module, "_run_extractors", fail_if_called)
    # Reset the throttle so the very next check reads the database.
    monkeypatch.setattr(cancellation, "CHECK_INTERVAL_SECONDS", 0.0)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "cancelled"  # not failed, not done
    event_types = [e["event_type"] for e in _workflow_events(client)]
    assert event_types[0] == "workflow.started"
    assert event_types[-1] == "workflow.cancelled"
    assert "workflow.failed" not in event_types
    assert "workflow.completed" not in event_types
    [cancelled] = [e for e in _workflow_events(client) if e["event_type"] == "workflow.cancelled"]
    assert cancelled["stage"] == "fetch"  # the stage that was open, never closed with stage.completed
    assert client.table("projects").rows == []  # a cancelled run never becomes a project's latest job


def test_process_job_stops_summarizing_when_cancelled(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    client.table("projects").insert({"id": "project-1", "org_id": "org-1", "source_type": "github", "source_ref": "x"}).execute()
    _seed_job(client, "job-1", "org-1")
    client.table("ingestion_jobs").rows[-1]["project_id"] = "project-1"
    monkeypatch.setattr(cancellation, "CHECK_INTERVAL_SECONDS", 0.0)

    def fake_run_extractors(repo_path, client_, job_id, org_id, emitter):
        _seed_entities(
            client_, job_id, org_id,
            [{"name": f"fn{i}", "file_path": "app.php", "content_hash": f"h{i}"} for i in range(3)],
        )

    monkeypatch.setattr(run_module, "_run_extractors", fake_run_extractors)
    _patch_grounded_pipeline(monkeypatch)
    summarized = []

    def flip_to_cancelled(*args, **kwargs):
        client.table("ingestion_jobs").rows[0]["status"] = "cancelled"

    # Cancel lands as the summarize stage starts; no entity should be summarized after it.
    original_enter = run_module._enter_stage

    def enter(emitter, client_, job_id, org_id, key):
        original_enter(emitter, client_, job_id, org_id, key)
        if key == "summarize":
            flip_to_cancelled()

    monkeypatch.setattr(run_module, "_enter_stage", enter)
    monkeypatch.setattr(run_module, "summarize_entity", lambda entity, client=None, excerpt=None: summarized.append(entity.name))

    run_module.process_job("job-1", "org-1")

    assert summarized == []
    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "cancelled"
    assert client.table("projects").rows[0].get("latest_job_id") is None
    assert [e["event_type"] for e in _workflow_events(client)][-1] == "workflow.cancelled"
