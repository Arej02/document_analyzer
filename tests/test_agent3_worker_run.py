"""Orchestration tests for the Agent 3 executor worker (agent3/worker/run.py).

Control flow only: stage ordering, status transitions, message deletion, loop
survival and the startup sandbox check. Stage functions are stubbed by
monkeypatching run.py's own module attributes, same approach as
tests/test_agent2_worker_run.py.
"""

import logging

import pytest

from document_analyzer.agent3.worker import assemble as assemble_module
from document_analyzer.agent3.worker import run as run_module
from document_analyzer.agent3.worker.sandbox import SandboxResult, SandboxUnavailableError
from document_analyzer.agent3.worker.status import (
    mark_execution_done,
    mark_execution_failed,
    mark_execution_running,
    mark_execution_stage,
)

from agent3_fixtures import ORG, seed_plan
from fakes import FakeClient

PARAMS = {
    "model_deployment": "gpt-test",
    "max_attempts": 3,
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 120,
    "sandbox_memory_mb": 1024,
    "runner_image": "galvanise-agent3-runner:0.1",
}

STAGES = ("prepare", "units", "assemble", "final_test")


def _seed_execution_job(client, execution_job_id="ej-1", org_id="org-1", status="queued"):
    client.table("execution_jobs").insert(
        {
            "id": execution_job_id,
            "org_id": org_id,
            "project_id": "project-1",
            "plan_id": "plan-1",
            "source_job_id": "job-1",
            "status": status,
            "progress_stage": None,
            "params": {"max_attempts": 3},
        }
    ).execute()


def _patch_stages(monkeypatch, client, calls, fail_at=None, exc=None):
    """Replaces every stage with a recorder that also captures the job's
    progress_stage at the moment the stage runs."""
    for name in STAGES:

        def stage(c, execution_job, state, _name=name):
            [row] = client.table("execution_jobs").rows
            calls.append((_name, row["status"], row["progress_stage"], execution_job["id"]))
            if _name == fail_at:
                raise exc

        monkeypatch.setattr(run_module, f"stage_{name}", stage)


def _patch_queue(monkeypatch, messages, deleted):
    async def fake_read():
        return messages.pop(0) if messages else None

    async def fake_delete(msg_id):
        deleted.append(msg_id)

    monkeypatch.setattr(run_module, "read_next_execution_job", fake_read)
    monkeypatch.setattr(run_module, "delete_execution_job_message", fake_delete)


@pytest.fixture
def client(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    return client


# --- process_execution_job --------------------------------------------------


def test_runs_stages_in_order_and_marks_done(monkeypatch, client):
    _seed_execution_job(client)
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_execution_job("ej-1", "org-1")

    assert calls == [(name, "running", name, "ej-1") for name in STAGES]
    [row] = client.table("execution_jobs").rows
    assert row["status"] == "done"
    assert row["progress_stage"] is None


def test_stages_share_one_state_object(monkeypatch, client):
    _seed_execution_job(client)
    seen = []
    for name in STAGES:
        monkeypatch.setattr(run_module, f"stage_{name}", lambda c, j, state: seen.append(state))

    run_module.process_execution_job("ej-1", "org-1")

    assert len(seen) == 4 and all(s is seen[0] for s in seen)
    assert isinstance(seen[0], run_module.ExecutionState)


def test_stage_failure_marks_failed_at_that_stage_and_stops(monkeypatch, client):
    _seed_execution_job(client)
    calls = []
    _patch_stages(monkeypatch, client, calls, fail_at="units", exc=SandboxUnavailableError("docker daemon is not reachable"))

    run_module.process_execution_job("ej-1", "org-1")

    assert [c[0] for c in calls] == ["prepare", "units"]
    [row] = client.table("execution_jobs").rows
    assert row["status"] == "failed"
    assert row["progress_stage"] == "units"
    assert row["error"] == "docker daemon is not reachable"


def test_exception_without_message_still_gets_a_readable_error(monkeypatch, client):
    _seed_execution_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="prepare", exc=KeyError())

    run_module.process_execution_job("ej-1", "org-1")

    [row] = client.table("execution_jobs").rows
    assert row["status"] == "failed"
    assert row["error"] == "KeyError"


def test_empty_plan_still_completes_the_full_pipeline(monkeypatch, client):
    """All four stages are real as of Phase 4 — nothing left to stub. An approved
    plan with zero migration_plan_items rows (an edge case, not an error:
    run_prepare_stage doesn't reject it) still runs prepare -> units (nothing to
    do) -> assemble (template only) -> final_test (only the template's own smoke
    tests run) and reaches `done`, proving no stage silently requires at least one
    unit. `sandbox.run_pytest` is monkeypatched so this needs no Docker."""
    job, plan, _project = seed_plan(client)
    client.table("execution_jobs").insert(
        {
            "id": "ej-1",
            "org_id": ORG,
            "project_id": "project-1",
            "plan_id": plan["id"],
            "source_job_id": job["id"],
            "status": "queued",
            "progress_stage": None,
            "params": PARAMS,
        }
    ).execute()
    monkeypatch.setattr(
        assemble_module,
        "run_pytest",
        lambda workspace, params: SandboxResult(
            status="completed", exit_code=0, tests=[], output_tail="", junit_xml="", duration_s=0.01
        ),
    )

    run_module.process_execution_job("ej-1", ORG)

    [row] = client.table("execution_jobs").rows
    assert row["status"] == "done"
    assert row["progress_stage"] is None
    assert row["output_zip_path"] == f"{ORG}/ej-1/project.zip"
    assert row["counts"] == {"total": 0, "done": 0, "passed": 0, "failed_verification": 0, "error": 0, "built_on_failed_dependency": 0}


def test_other_orgs_job_is_never_touched(monkeypatch, client):
    _seed_execution_job(client, org_id="org-A")
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_execution_job("ej-1", "org-B")

    assert calls == []
    [row] = client.table("execution_jobs").rows
    assert row["status"] == "queued"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_redelivered_message_for_a_finished_job_is_skipped(monkeypatch, client, status):
    _seed_execution_job(client, status=status)
    calls = []
    _patch_stages(monkeypatch, client, calls)

    run_module.process_execution_job("ej-1", "org-1")

    assert calls == []
    [row] = client.table("execution_jobs").rows
    assert row["status"] == status


# --- run_once: message delete in finally ------------------------------------


def test_run_once_returns_false_on_empty_queue(monkeypatch, client):
    deleted = []
    _patch_queue(monkeypatch, [], deleted)

    assert run_module.run_once() is False
    assert deleted == []


def test_run_once_deletes_message_after_success(monkeypatch, client):
    _seed_execution_job(client)
    _patch_stages(monkeypatch, client, [])
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "execution_job_id": "ej-1", "org_id": "org-1"}], deleted)

    assert run_module.run_once() is True
    assert deleted == [7]
    assert client.table("execution_jobs").rows[0]["status"] == "done"


def test_run_once_marks_failed_and_still_deletes_message(monkeypatch, client):
    _seed_execution_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="assemble", exc=RuntimeError("storage down"))
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "execution_job_id": "ej-1", "org_id": "org-1"}], deleted)

    assert run_module.run_once() is True
    assert deleted == [7]
    [row] = client.table("execution_jobs").rows
    assert row["status"] == "failed"
    assert row["error"] == "storage down"


def test_run_once_deletes_message_even_if_marking_failed_raises(monkeypatch, client):
    _seed_execution_job(client)
    _patch_stages(monkeypatch, client, [], fail_at="prepare", exc=RuntimeError("stage broke"))

    def broken_mark_failed(*args, **kwargs):
        raise ConnectionError("db unreachable")

    monkeypatch.setattr(run_module, "mark_execution_failed", broken_mark_failed)
    deleted = []
    _patch_queue(monkeypatch, [{"msg_id": 7, "execution_job_id": "ej-1", "org_id": "org-1"}], deleted)

    with pytest.raises(ConnectionError):
        run_module.run_once()
    assert deleted == [7]


# --- run_forever: startup checks and loop survival --------------------------


class _StopLoop(BaseException):
    """BaseException so the loop's `except Exception` can't swallow it."""


def test_unexpected_error_does_not_crash_the_loop(monkeypatch):
    monkeypatch.setattr(run_module, "check_sandbox_at_startup", lambda: None)
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


def test_run_forever_refuses_to_start_without_the_sandbox(monkeypatch):
    def unavailable(params, require_gvisor):
        raise SandboxUnavailableError("docker daemon is not reachable")

    monkeypatch.setattr(run_module, "check_sandbox_available", unavailable)
    monkeypatch.setattr(run_module, "run_once", lambda: pytest.fail("polled without a sandbox"))

    with pytest.raises(SandboxUnavailableError):
        run_module.run_forever()


def test_startup_passes_the_env_sandbox_params_and_gvisor_requirement(monkeypatch):
    monkeypatch.setenv("AGENT3_RUNNER_IMAGE", "img:9")
    monkeypatch.setenv("AGENT3_SANDBOX_RUNTIME", "runsc")
    monkeypatch.setenv("AGENT3_REQUIRE_GVISOR", "true")
    seen = []
    monkeypatch.setattr(run_module, "check_sandbox_available", lambda params, require_gvisor: seen.append((params, require_gvisor)))

    run_module.check_sandbox_at_startup()

    [(params, require_gvisor)] = seen
    assert (params.image, params.runtime, require_gvisor) == ("img:9", "runsc", True)


def test_startup_warns_without_gvisor(monkeypatch, caplog):
    monkeypatch.delenv("AGENT3_SANDBOX_RUNTIME", raising=False)
    monkeypatch.delenv("AGENT3_REQUIRE_GVISOR", raising=False)
    monkeypatch.setattr(run_module, "check_sandbox_available", lambda params, require_gvisor: None)

    with caplog.at_level(logging.WARNING, logger=run_module.__name__):
        run_module.check_sandbox_at_startup()

    assert "without gVisor" in caplog.text


def test_startup_does_not_warn_with_gvisor(monkeypatch, caplog):
    monkeypatch.setenv("AGENT3_SANDBOX_RUNTIME", "runsc")
    monkeypatch.setattr(run_module, "check_sandbox_available", lambda params, require_gvisor: None)

    with caplog.at_level(logging.WARNING, logger=run_module.__name__):
        run_module.check_sandbox_at_startup()

    assert "without gVisor" not in caplog.text


# --- status helpers: id + org_id filter -------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: mark_execution_running(c, "ej-1", "org-B"),
        lambda c: mark_execution_stage(c, "ej-1", "org-B", "units"),
        lambda c: mark_execution_failed(c, "ej-1", "org-B", "boom"),
        lambda c: mark_execution_done(c, "ej-1", "org-B"),
    ],
)
def test_status_helpers_do_not_touch_another_orgs_job(call):
    client = FakeClient(org_id="org-A")
    _seed_execution_job(client, org_id="org-A")

    call(client)

    [row] = client.table("execution_jobs").rows
    assert row["status"] == "queued"
    assert row["progress_stage"] is None
    assert "error" not in row
