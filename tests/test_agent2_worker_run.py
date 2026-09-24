"""Orchestration tests for the Agent 2 planner worker (agent2/worker/run.py).

Control flow only: stage ordering, status transitions, message deletion and
loop survival. Stage functions are stubbed by monkeypatching run.py's own
module attributes, same approach as tests/test_worker_run.py.
"""

import pytest

from document_analyzer.agent2.worker import run as run_module
from document_analyzer.agent2.worker.status import (
    mark_planning_done,
    mark_planning_failed,
    mark_planning_running,
    mark_planning_stage,
)

from fakes import FakeClient


def _seed_planning_job(client, planning_job_id="pj-1", org_id="org-1", status="queued"):
    client.table("planning_jobs").insert(
        {
            "id": planning_job_id,
            "org_id": org_id,
            "project_id": "project-1",
            "source_job_id": "job-1",
            "status": status,
            "progress_stage": None,
            "params": {"seed": 42},
        }
    ).execute()


def _patch_stages(monkeypatch, client, calls, fail_at=None, exc=None):
    """Replaces every stage with a recorder that also captures the job's
    progress_stage at the moment the stage runs."""
    for name in ("graph", "cluster", "report", "order"):

        def stage(c, planning_job, state, _name=name):
            [row] = client.table("planning_jobs").rows
            calls.append((_name, row["status"], row["progress_stage"], planning_job["id"]))
            if _name == fail_at:
                raise exc

        monkeypatch.setattr(run_module, f"stage_{name}", stage)


def _patch_queue(monkeypatch, messages, deleted):
    async def fake_read():
        return messages.pop(0) if messages else None

    async def fake_delete(msg_id):
        deleted.append(msg_id)

    monkeypatch.setattr(run_module, "read_next_planning_job", fake_read)
    monkeypatch.setattr(run_module, "delete_planning_job_message", fake_delete)


@pytest.fixture
def client(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    return client


# --- process_planning_job ---------------------------------------------------


def test_runs_stages_in_order_and_marks_done(monkeypatch, client):
    _seed_planning_job(client)
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_planning_job("pj-1", "org-1")

    assert calls == [
        ("graph", "running", "graph", "pj-1"),
        ("cluster", "running", "cluster", "pj-1"),
        ("report", "running", "report", "pj-1"),
        ("order", "running", "order", "pj-1"),
    ]
    [row] = client.table("planning_jobs").rows
    assert row["status"] == "done"
    assert row["progress_stage"] is None


def test_stage_failure_marks_failed_at_that_stage_and_stops(monkeypatch, client):
    _seed_planning_job(client)
    calls = []
    _patch_stages(monkeypatch, client, calls, fail_at="cluster", exc=ValueError("leiden blew up"))

    run_module.process_planning_job("pj-1", "org-1")

    assert [c[0] for c in calls] == ["graph", "cluster"]
    [row] = client.table("planning_jobs").rows
    assert row["status"] == "failed"
    assert row["progress_stage"] == "cluster"
    assert row["error"] == "leiden blew up"


def test_exception_without_message_still_gets_a_readable_error(monkeypatch, client):
    _seed_planning_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="graph", exc=KeyError())

    run_module.process_planning_job("pj-1", "org-1")

    [row] = client.table("planning_jobs").rows
    assert row["status"] == "failed"
    assert row["error"] == "KeyError"


def test_other_orgs_job_is_never_touched(monkeypatch, client):
    _seed_planning_job(client, org_id="org-A")
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_planning_job("pj-1", "org-B")

    assert calls == []
    [row] = client.table("planning_jobs").rows
    assert row["status"] == "queued"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_redelivered_message_for_a_finished_job_is_skipped(monkeypatch, client, status):
    _seed_planning_job(client, status=status)
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_planning_job("pj-1", "org-1")

    assert calls == []
    [row] = client.table("planning_jobs").rows
    assert row["status"] == status


# --- run_once: message delete in finally ------------------------------------


def test_run_once_returns_false_on_empty_queue(monkeypatch, client):
    deleted = []
    _patch_queue(monkeypatch, [], deleted)

    assert run_module.run_once() is False
    assert deleted == []


def test_run_once_deletes_message_after_success(monkeypatch, client):
    _seed_planning_job(client)
    _patch_stages(monkeypatch, client, [])
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "planning_job_id": "pj-1", "org_id": "org-1"}], deleted)

    assert run_module.run_once() is True
    assert deleted == [7]
    assert client.table("planning_jobs").rows[0]["status"] == "done"


def test_run_once_marks_failed_and_still_deletes_message(monkeypatch, client):
    _seed_planning_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="report", exc=RuntimeError("llm down"))
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "planning_job_id": "pj-1", "org_id": "org-1"}], deleted)

    assert run_module.run_once() is True
    assert deleted == [7]
    [row] = client.table("planning_jobs").rows
    assert row["status"] == "failed"
    assert row["error"] == "llm down"


def test_run_once_deletes_message_even_if_marking_failed_raises(monkeypatch, client):
    _seed_planning_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="graph", exc=RuntimeError("stage broke"))

    def broken_mark_failed(*args, **kwargs):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(run_module, "mark_planning_failed", broken_mark_failed)
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "planning_job_id": "pj-1", "org_id": "org-1"}], deleted)

    with pytest.raises(ConnectionError):
        run_module.run_once()
    assert deleted == [7]


# --- run_forever: an unexpected error doesn't crash the loop ----------------


class _StopLoop(BaseException):
    """BaseException so the loop's `except Exception` can't swallow it."""


def test_unexpected_error_does_not_crash_the_loop(monkeypatch):
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: object())
    monkeypatch.setattr(run_module, "assert_embedding_config_in_sync", lambda client: None)
    monkeypatch.setattr(run_module.time, "sleep", lambda s: None)

    outcomes = iter([RuntimeError("pgmq read failed"), True, ConnectionError("delete failed"), _StopLoop()])
    iterations = []

    def fake_run_once():
        iterations.append(1)
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(run_module, "run_once", fake_run_once)

    with pytest.raises(_StopLoop):
        run_module.run_forever()
    assert len(iterations) == 4  # kept going after both errors


def test_run_forever_refuses_to_start_on_embedding_config_drift(monkeypatch):
    class Drift(Exception):
        pass

    def drifted(client):
        raise Drift("dimension mismatch")

    monkeypatch.setattr(run_module, "get_service_role_client", lambda: object())
    monkeypatch.setattr(run_module, "assert_embedding_config_in_sync", drifted)
    monkeypatch.setattr(run_module, "run_once", lambda: pytest.fail("polled despite drift"))

    with pytest.raises(Drift):
        run_module.run_forever()


# --- status helpers: id + org_id filter -------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: mark_planning_running(c, "pj-1", "org-B"),
        lambda c: mark_planning_stage(c, "pj-1", "org-B", "cluster"),
        lambda c: mark_planning_failed(c, "pj-1", "org-B", "boom"),
        lambda c: mark_planning_done(c, "pj-1", "org-B"),
    ],
)
def test_status_helpers_do_not_touch_another_orgs_job(call):
    client = FakeClient(org_id="org-A")
    _seed_planning_job(client, org_id="org-A")

    call(client)

    [row] = client.table("planning_jobs").rows
    assert row["status"] == "queued"
    assert row["progress_stage"] is None
    assert "error" not in row
