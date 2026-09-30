"""shared/workflow_events.py::WorkflowEventEmitter — design.md §2, tasks.md
"Live workflow run view"."""

import threading

import pytest

from document_analyzer.shared import workflow_events as workflow_events_module
from document_analyzer.shared.workflow_events import FLUSH_BATCH_SIZE, WorkflowEventEmitter

from fakes import FakeClient


class _FakeClock:
    """A controllable stand-in for time.monotonic(), so the ~1s flush interval
    can be tested without an actual sleep."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(workflow_events_module.time, "monotonic", fake)
    return fake


def _rows(client):
    return client.table("workflow_events").rows


def test_rejects_an_unknown_workflow_kind():
    with pytest.raises(ValueError, match="agent1_ingestion"):
        WorkflowEventEmitter(FakeClient(), "not_a_real_kind", "job-1", "org-123")


# --- lifecycle / stage shape -------------------------------------------------


def test_start_flushes_immediately_with_declared_stages(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.start([{"key": "fetch", "label": "Fetch"}, {"key": "extract", "label": "Extract"}])

    [row] = _rows(client)
    assert row["event_type"] == "workflow.started"
    assert row["data"]["stages"] == [{"key": "fetch", "label": "Fetch"}, {"key": "extract", "label": "Extract"}]
    assert row["stage"] is None


def test_stage_transition_closes_the_previous_stage_and_opens_the_next(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.stage("fetch")
    emitter.stage("extract")

    events = [(row["event_type"], row["stage"]) for row in _rows(client)]
    assert events == [
        ("stage.started", "fetch"),
        ("stage.completed", "fetch"),
        ("stage.started", "extract"),
    ]


def test_complete_closes_the_open_stage_then_emits_workflow_completed(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.stage("fold")

    emitter.complete()

    events = [(row["event_type"], row["stage"]) for row in _rows(client)]
    assert events[-2:] == [("stage.completed", "fold"), ("workflow.completed", None)]


def test_fail_tags_the_stage_that_was_open_and_does_not_close_it(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.stage("summarize")

    emitter.fail("Azure OpenAI request failed")

    events = [(row["event_type"], row["stage"]) for row in _rows(client)]
    assert ("stage.completed", "summarize") not in events
    assert events[-1] == ("workflow.failed", "summarize")
    assert _rows(client)[-1]["data"]["error"] == "Azure OpenAI request failed"


def test_activity_carries_the_current_stage_level_and_refs(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.stage("extract")

    emitter.activity(
        "entity summary unverified", refs=[{"kind": "entity", "value": "checkout()"}], level="warn"
    )
    emitter.flush()

    activity_row = _rows(client)[-1]
    assert activity_row["event_type"] == "activity"
    assert activity_row["stage"] == "extract"
    assert activity_row["message"] == "entity summary unverified"
    assert activity_row["data"] == {"level": "warn", "refs": [{"kind": "entity", "value": "checkout()"}]}


def test_activity_defaults_to_info_level_and_empty_refs(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.activity("cloned repo")
    emitter.flush()

    assert _rows(client)[-1]["data"] == {"level": "info", "refs": []}


def test_output_is_created_once_then_updated(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.output("snapshot", kind="snapshot", label="Source snapshot", status="pending")
    emitter.output("snapshot", kind="snapshot", label="Source snapshot", status="done")
    emitter.flush()

    event_types = [row["event_type"] for row in _rows(client)]
    assert event_types == ["output.created", "output.updated"]
    assert _rows(client)[-1]["data"] == {
        "output_id": "snapshot",
        "kind": "snapshot",
        "label": "Source snapshot",
        "status": "done",
    }


def test_stats_updated_carries_a_flat_map(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.stats(changed_files=12, unchanged_files=88)
    emitter.flush()

    row = _rows(client)[-1]
    assert row["event_type"] == "stats.updated"
    assert row["data"] == {"changed_files": 12, "unchanged_files": 88}


def test_progress_updated_carries_the_stage_and_counts(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.stage("summarize")

    emitter.progress(3, 10, "entities", "src/app.php")
    emitter.flush()

    row = _rows(client)[-1]
    assert row["event_type"] == "progress.updated"
    assert row["stage"] == "summarize"
    assert row["data"] == {"done": 3, "total": 10, "unit": "entities", "current_path": "src/app.php"}


def test_progress_is_throttled_but_the_closing_event_always_goes(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.stage("summarize")

    emitter.progress(1, 100, "entities")      # sent: nothing sent before it
    clock.advance(0.2)
    emitter.progress(2, 100, "entities")      # dropped: inside the 1 s window
    emitter.progress(3, 100, "entities")      # dropped
    clock.advance(1.0)
    emitter.progress(4, 100, "entities")      # sent: window elapsed
    emitter.progress(100, 100, "entities")    # sent: the closing event ignores the throttle
    emitter.flush()

    sent = [r["data"]["done"] for r in _rows(client) if r["event_type"] == "progress.updated"]
    assert sent == [1, 4, 100]


def test_progress_with_no_total_is_never_treated_as_final(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.progress(0, 0, "entities")
    emitter.progress(0, 0, "entities")  # total 0 is not "done": still throttled
    emitter.flush()

    assert len([r for r in _rows(client) if r["event_type"] == "progress.updated"]) == 1


# --- batching -----------------------------------------------------------------


def test_buffers_activity_events_below_the_batch_size(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    for _ in range(FLUSH_BATCH_SIZE - 1):
        emitter.activity("progress")

    assert _rows(client) == []


def test_flushes_automatically_once_the_batch_size_is_reached(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    for _ in range(FLUSH_BATCH_SIZE):
        emitter.activity("progress")

    assert len(_rows(client)) == FLUSH_BATCH_SIZE


def test_flushes_automatically_once_the_time_interval_elapses(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.activity("first")
    assert _rows(client) == []

    clock.advance(1.5)
    emitter.activity("second")

    assert len(_rows(client)) == 2


def test_explicit_flush_writes_a_partial_buffer(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    emitter.activity("only one")

    emitter.flush()

    assert len(_rows(client)) == 1


def test_flush_with_an_empty_buffer_writes_nothing(clock):
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.flush()

    assert _rows(client) == []


# --- concurrency ----------------------------------------------------------------


def test_concurrent_emits_from_multiple_threads_are_not_lost_or_corrupted():
    """summarize_fanout.py runs entity summarization in a ThreadPoolExecutor
    (specs.md NFR5), so many threads call activity()/stats() on the same
    emitter at once. Every event from every thread must survive."""
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")
    thread_count, events_per_thread = 20, 25

    def worker(thread_index):
        for i in range(events_per_thread):
            emitter.activity(f"thread {thread_index} event {i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    emitter.flush()

    messages = {row["message"] for row in _rows(client)}
    assert len(_rows(client)) == thread_count * events_per_thread
    assert messages == {f"thread {i} event {j}" for i in range(thread_count) for j in range(events_per_thread)}


# --- failure handling / org stamping ------------------------------------------


def test_a_failing_insert_does_not_raise(monkeypatch, caplog):
    client = FakeClient()

    def _broken_insert(_row):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(client.table("workflow_events"), "insert", _broken_insert)
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.start([{"key": "fetch", "label": "Fetch"}])  # force_flush=True — must not raise

    assert "dropped" in caplog.text


def test_a_failing_insert_drops_the_batch_rather_than_retrying(monkeypatch):
    client = FakeClient()
    call_count = 0

    def _broken_insert(_row):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(client.table("workflow_events"), "insert", _broken_insert)
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    emitter.start([{"key": "fetch", "label": "Fetch"}])
    emitter.flush()  # buffer was already cleared by start()'s forced flush; nothing left to retry

    assert call_count == 1


def test_an_internal_bug_logs_and_drops_rather_than_raising(monkeypatch, caplog):
    """Not just flush()'s network path — any exception inside a public method
    logs and drops rather than raising, per design.md "Event writes never fail
    a job"."""
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-123")

    def _broken_append(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(emitter, "_append", _broken_append)

    result = emitter.activity("this should never raise")

    assert result is None
    assert "activity" in caplog.text


def test_org_id_is_stamped_from_the_constructor_argument():
    client = FakeClient(org_id="org-ambient")
    emitter = WorkflowEventEmitter(client, "agent1_ingestion", "job-1", "org-explicit")

    emitter.activity("hello")
    emitter.flush()

    assert _rows(client)[-1]["org_id"] == "org-explicit"


def test_every_row_carries_workflow_kind_and_workflow_id():
    client = FakeClient()
    emitter = WorkflowEventEmitter(client, "agent2_planning", "planning-job-9", "org-123")

    emitter.activity("clustering")
    emitter.flush()

    row = _rows(client)[-1]
    assert row["workflow_kind"] == "agent2_planning"
    assert row["workflow_id"] == "planning-job-9"
