"""Live workflow run view — design.md §2 (`workflow_events`) and tasks.md
"Live workflow run view".

`WorkflowEventEmitter` is how a worker writes its append-only progress stream.
It lives in `shared/` (not `worker/`) because all three agents' workers use
it against the one shared table, each stamping only its own `workflow_kind`
— importing this pure helper (no pipeline state, no agent-specific logic) is
not an agent-to-agent call (CLAUDE.md "Core architectural rules").

Buffering: events are appended to an in-memory buffer under a lock and only
actually written on `flush()` — called automatically once ~50 events have
piled up or ~1 s has passed since the last flush, and forced immediately for
the milestone events (`start`/`stage`/`complete`/`fail`) so the run view
never waits a full second to see them. The buffering exists because entity
summarization fans out across a `ThreadPoolExecutor` (specs.md NFR5) and
would otherwise be one INSERT per entity.

Never raises: every public method logs and drops on failure rather than
propagating, same as a grounding-check failure never blocks the job
(CLAUDE.md, design.md's "Event writes never fail a job"). A flush that fails
mid-network-call still drops exactly the batch it took off the buffer — kept
simple rather than requeued, matching the "no worse than a lost log line"
severity of an event write.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Callable

from supabase import Client

from document_analyzer.supabase_client import insert_many_scoped

logger = logging.getLogger(__name__)

# Same three values as the workflow_events.workflow_kind check constraint
# (supabase/migrations/20260928030000_workflow_events.sql).
VALID_WORKFLOW_KINDS = ("agent1_ingestion", "agent2_planning", "agent3_execution")

FLUSH_BATCH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 1.0
PROGRESS_MIN_INTERVAL_SECONDS = 1.0


def _logs_and_drops(method: Callable) -> Callable:
    """Wraps a public method so it never raises into the caller — logs a warning
    and returns None instead. Applied to every public method, not just flush(),
    since a bug in event construction is exactly as unworthy of failing a job as
    a network error writing it."""

    @functools.wraps(method)
    def wrapper(self: "WorkflowEventEmitter", *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            logger.warning(
                "workflow event emitter: %s() failed for %s %s (org %s)",
                method.__name__,
                self._kind,
                self._workflow_id,
                self._org_id,
                exc_info=True,
            )
            return None

    return wrapper


class WorkflowEventEmitter:
    """Buffers and writes `workflow_events` rows for one running workflow.

    `client` is the service-role client (writes are service-role only, §2).
    `kind` is one of VALID_WORKFLOW_KINDS. `workflow_id` is that agent's own
    job row id (e.g. `ingestion_jobs.id`). `org_id` is stamped onto every row
    from the job row, never from repo content — the same caller-supplied,
    already-resolved org_id every other worker write in this codebase uses
    (`supabase_client.py::insert_scoped`'s own docstring).
    """

    def __init__(self, client: Client, kind: str, workflow_id: str, org_id: str):
        if kind not in VALID_WORKFLOW_KINDS:
            raise ValueError(f"kind must be one of {VALID_WORKFLOW_KINDS}, got {kind!r}")
        self._client = client
        self._kind = kind
        self._workflow_id = workflow_id
        self._org_id = org_id

        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._last_flush = time.monotonic()
        self._last_progress_at = float("-inf")
        self._stage: str | None = None
        self._seen_outputs: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    @_logs_and_drops
    def start(self, stages: list[dict]) -> None:
        """`stages` = [{key, label}, ...] — the workflow declares its own stages,
        so the UI never hardcodes them (design.md §2)."""
        self._append("workflow.started", data={"stages": stages}, force_flush=True)

    @_logs_and_drops
    def stage(self, key: str) -> None:
        """Moves into stage `key`: closes out whichever stage was open (if any)
        with `stage.completed`, then opens the new one with `stage.started`."""
        with self._lock:
            previous, self._stage = self._stage, key
        if previous is not None:
            self._append("stage.completed", stage=previous, force_flush=True)
        self._append("stage.started", stage=key, force_flush=True)

    @_logs_and_drops
    def complete(self) -> None:
        """Closes out the open stage (if any), then emits `workflow.completed`."""
        with self._lock:
            previous, self._stage = self._stage, None
        if previous is not None:
            self._append("stage.completed", stage=previous, force_flush=True)
        self._append("workflow.completed", force_flush=True)

    @_logs_and_drops
    def fail(self, error: str) -> None:
        """Emits `workflow.failed` tagged with whichever stage was open when it
        failed. Deliberately doesn't emit `stage.completed` first — the stage
        didn't complete, it's where the job died."""
        self._append("workflow.failed", stage=self._current_stage(), data={"error": str(error)}, force_flush=True)

    @_logs_and_drops
    def cancel(self) -> None:
        """Emits `workflow.cancelled` tagged with whichever stage was open. Like `fail`, it does
        not close the open stage: that stage did not complete."""
        self._append("workflow.cancelled", stage=self._current_stage(), force_flush=True)

    # -- ongoing progress ------------------------------------------------------

    @_logs_and_drops
    def activity(self, message: str, refs: list[dict] | None = None, level: str = "info") -> None:
        """`refs` = [{kind: file|repo|entity|id|commit, value}, ...] for the UI to
        highlight; `level` is info|warn|error."""
        self._append(
            "activity",
            stage=self._current_stage(),
            message=message,
            data={"level": level, "refs": refs or []},
        )

    @_logs_and_drops
    def output(self, output_id: str, kind: str, label: str, status: str) -> None:
        """`output.created` the first time this output_id is seen, `output.updated`
        on every call after — the UI keys its outputs list by output_id and
        updates in place (frontend's workflowReducer.js, tasks.md)."""
        with self._lock:
            event_type = "output.updated" if output_id in self._seen_outputs else "output.created"
            self._seen_outputs.add(output_id)
        self._append(
            event_type,
            stage=self._current_stage(),
            data={"output_id": output_id, "kind": kind, "label": label, "status": status},
        )

    @_logs_and_drops
    def stats(self, **counts) -> None:
        """A flat {name: number} map. The client merges maps across events, so
        this only ever needs to send whatever changed, not a running total."""
        self._append("stats.updated", stage=self._current_stage(), data=counts)

    @_logs_and_drops
    def progress(self, done: int, total: int, unit: str, current_path: str | None = None) -> None:
        """`progress.updated` for the open stage: `done` of `total` `unit`s, and what is being
        worked on. Throttled to one event per PROGRESS_MIN_INTERVAL_SECONDS (the summarizer would
        otherwise emit one per entity), except the closing event (done >= total), which always goes."""
        final = total > 0 and done >= total
        now = time.monotonic()
        with self._lock:
            if not final and now - self._last_progress_at < PROGRESS_MIN_INTERVAL_SECONDS:
                return
            self._last_progress_at = now
        self._append(
            "progress.updated",
            stage=self._current_stage(),
            data={"done": done, "total": total, "unit": unit, "current_path": current_path},
        )

    # -- flushing ----------------------------------------------------------

    @_logs_and_drops
    def flush(self) -> None:
        """Writes the buffered batch and clears it, whether or not the write
        succeeds — a failed insert drops that batch rather than retrying it
        (design.md "Event writes never fail a job")."""
        with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
            self._last_flush = time.monotonic()
        try:
            insert_many_scoped(self._client, "workflow_events", self._org_id, batch)
        except Exception:
            logger.warning(
                "workflow event flush dropped %d event(s) for %s %s (org %s)",
                len(batch),
                self._kind,
                self._workflow_id,
                self._org_id,
                exc_info=True,
            )

    # -- internals -----------------------------------------------------------

    def _current_stage(self) -> str | None:
        with self._lock:
            return self._stage

    def _append(
        self,
        event_type: str,
        *,
        stage: str | None = None,
        message: str | None = None,
        data: dict | None = None,
        force_flush: bool = False,
    ) -> None:
        row = {
            "workflow_kind": self._kind,
            "workflow_id": self._workflow_id,
            "event_type": event_type,
            "stage": stage,
            "message": message,
            "data": data if data is not None else {},
        }
        with self._lock:
            self._buffer.append(row)
            due = (
                force_flush
                or len(self._buffer) >= FLUSH_BATCH_SIZE
                or (time.monotonic() - self._last_flush) >= FLUSH_INTERVAL_SECONDS
            )
        if due:
            self.flush()
