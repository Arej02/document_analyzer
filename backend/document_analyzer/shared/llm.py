"""The single choke point for Azure OpenAI — design.md §7.3.

Everything that talks to Azure OpenAI goes through here, so every call is measured
and recorded in `llm_invocations` (tokens, estimated cost, latency). Nothing else in
`backend/document_analyzer/` may import `openai` or construct `AzureOpenAI` (a guard
test enforces this).

What's here:
- `get_azure_openai_client()`: the one client factory (re-exported by
  `worker/summarize.py` so existing imports keep working).
- `LLMClient`: a protocol for type hints, in place of `AzureOpenAI`.
- `MeteredLLMClient`: wraps a client with the same `.chat.completions.create` shape.
  `bind(...)` returns a new immutable client with extra context, which is how a call
  site says what it is for (purpose, entity, unit, attempt, round, retry reason).
  Context lives on the client object, not in contextvars, because summaries and
  reports run in a `ThreadPoolExecutor` and contextvars don't cross into pool threads.
- `LLMCostRecorder`: a thread-safe buffer that writes the rows.

Cost logging is best effort and never blocks work. Recording a call, flushing, and
closing never raise into the caller: a failure logs a warning (with the dropped row
count) and moves on. A hard kill loses only the current unflushed buffer (at most
`FLUSH_BATCH_SIZE` rows or `FLUSH_INTERVAL_SECONDS` of calls). Prompt and response
text is never recorded, and an error call records only the exception class name.

`org_id` is given to the recorder from the job row (or `AuthContext` for Q&A), never
from repository content, and is stamped on every row by the recorder alone.

`max_retries` is set explicitly on the SDK client. The SDK's own 429/5xx retries are
invisible to us (one `create()` call, however many HTTP attempts), and their backoff
counts toward `latency_ms`. That is deliberate: seeing each retry would need
`max_retries=0` plus a retry loop of our own.

MiniLM embeddings are local and are not recorded.
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import os
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from openai import AzureOpenAI
from supabase import Client

from document_analyzer.shared.llm_pricing import price_call
from document_analyzer.supabase_client import insert_many_scoped

logger = logging.getLogger(__name__)

# The SDK default, made explicit (see module docstring).
AZURE_MAX_RETRIES = 2

FLUSH_BATCH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 5.0

_AGENT_JOB_TYPES = {
    "agent1": ("ingestion", "qa"),
    "agent2": ("planning",),
    "agent3": ("execution",),
    "report": ("report",),
}


def get_azure_openai_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        max_retries=AZURE_MAX_RETRIES,
    )


class _Completions(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _Chat(Protocol):
    completions: _Completions


class LLMClient(Protocol):
    """What call sites need from a client: `client.chat.completions.create(**kwargs)`.
    Both `AzureOpenAI` and `MeteredLLMClient` satisfy it."""

    chat: _Chat


def new_run_id() -> str:
    """One per worker pickup, so a pgmq redelivery that runs a job twice stays separable."""
    return str(uuid.uuid4())


@dataclasses.dataclass(frozen=True)
class CallContext:
    """Everything a row records except what the response tells us.

    `agent` / `job_type` / `job_id` / `run_id` / `source_job_id` / `requested_by` come
    from the worker or request. The rest is narrowed per call site with `bind()`.
    `job_id` is None only for Q&A (a request, not a job)."""

    agent: str
    job_type: str
    job_id: str | None = None
    run_id: str | None = None
    source_job_id: str | None = None
    requested_by: str | None = None
    stage: str | None = None
    purpose: str = "unspecified"
    entity_id: str | None = None
    cluster_id: str | None = None
    attempt: int | None = None
    round: int | None = None
    retry_reason: str | None = None

    def __post_init__(self) -> None:
        if self.job_type not in _AGENT_JOB_TYPES.get(self.agent, ()):
            raise ValueError(f"job_type {self.job_type!r} is not valid for agent {self.agent!r}")
        if (self.job_type == "qa") != (self.job_id is None):
            raise ValueError("job_id must be None for qa and set for every other job_type")


_BINDABLE = frozenset(
    {"stage", "purpose", "entity_id", "cluster_id", "attempt", "round", "retry_reason"}
)


def _usage_fields(response: Any) -> dict[str, int | None]:
    """Token counts from `response.usage`, each None when the API didn't report it."""
    usage = getattr(response, "usage", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "cached_prompt_tokens": getattr(prompt_details, "cached_tokens", None),
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


class LLMCostRecorder:
    """Thread-safe buffer of `llm_invocations` rows for one job run (or one Q&A request).

    `client` is the service-role Supabase client (both cost tables are service-role
    only). `org_id` is the job row's org (or the caller's, for Q&A).

    Flushes happen when the buffer reaches `FLUSH_BATCH_SIZE` rows or
    `FLUSH_INTERVAL_SECONDS` have passed (checked when a row is recorded, no timer
    thread), and whenever the owner calls `flush()`: at the end of each stage, and
    in a `finally` on success, failure and `JobCancelled`. `close()` flushes and
    detaches; a process-wide `atexit` hook flushes any recorder still open.

    A flush upserts `llm_job_costs` from in-memory totals. To survive a redelivered
    job (a second run of the same job in a new recorder), the first flush for a job
    reads the existing rollup row as its baseline. Q&A has no rollup row.
    """

    def __init__(
        self,
        client: Client,
        org_id: str,
        *,
        batch_size: int = FLUSH_BATCH_SIZE,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
    ):
        self._client = client
        self._org_id = org_id
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._lock = threading.Lock()  # guards _buffer, _totals, _last_flush
        self._flush_lock = threading.Lock()  # serializes flushes so rollup upserts stay ordered
        self._buffer: list[dict] = []
        self._totals: dict[tuple[str, str, str], dict] = {}
        self._baselines: dict[tuple[str, str, str], dict] = {}
        self._last_flush = time.monotonic()
        self._closed = False
        _OPEN_RECORDERS.add(self)

    @property
    def org_id(self) -> str:
        return self._org_id

    def record(self, row: dict) -> None:
        """Buffer one row. Never raises."""
        try:
            with self._lock:
                self._buffer.append(row)
                self._add_to_totals(row)
                due = (
                    len(self._buffer) >= self._batch_size
                    or time.monotonic() - self._last_flush >= self._flush_interval
                )
            if due:
                self.flush()
        except Exception:
            logger.warning("llm cost recorder: record() failed (org %s)", self._org_id, exc_info=True)

    def _add_to_totals(self, row: dict) -> None:
        if row.get("job_id") is None:
            return
        key = (row["agent"], row["job_type"], row["job_id"])
        t = self._totals.setdefault(
            key, {"tokens": 0, "cost_usd": Decimal(0), "calls": 0, "error_calls": 0}
        )
        t["calls"] += 1
        t["tokens"] += row.get("total_tokens") or 0
        if row.get("cost_usd") is not None:
            t["cost_usd"] += row["cost_usd"]
        if row["status"] == "error":
            t["error_calls"] += 1

    def flush(self) -> None:
        """Write buffered rows and upsert rollups. Never raises; a failure drops
        the rows it took off the buffer and logs how many."""
        try:
            with self._flush_lock:
                with self._lock:
                    rows, self._buffer = self._buffer, []
                    self._last_flush = time.monotonic()
                    totals = {k: dict(v) for k, v in self._totals.items()}
                if rows:
                    self._insert_rows(rows)
                self._upsert_rollups(totals)
        except Exception:
            logger.warning("llm cost recorder: flush() failed (org %s)", self._org_id, exc_info=True)

    def _insert_rows(self, rows: list[dict]) -> None:
        payload = [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows]
        try:
            insert_many_scoped(self._client, "llm_invocations", self._org_id, payload)
        except Exception:
            logger.warning(
                "llm cost recorder: dropped %d llm_invocations row(s) (org %s)",
                len(rows),
                self._org_id,
                exc_info=True,
            )

    def _upsert_rollups(self, totals: dict[tuple[str, str, str], dict]) -> None:
        for key, t in totals.items():
            agent, job_type, job_id = key
            try:
                base = self._baselines.get(key)
                if base is None:
                    base = self._load_baseline(job_type, job_id)
                    self._baselines[key] = base
                self._client.table("llm_job_costs").upsert(
                    {
                        "org_id": self._org_id,
                        "agent": agent,
                        "job_type": job_type,
                        "job_id": job_id,
                        "tokens": base["tokens"] + t["tokens"],
                        "cost_usd": str(base["cost_usd"] + t["cost_usd"]),
                        "calls": base["calls"] + t["calls"],
                        "error_calls": base["error_calls"] + t["error_calls"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="job_type,job_id",
                ).execute()
            except Exception:
                logger.warning(
                    "llm cost recorder: rollup upsert failed for %s %s (org %s)",
                    job_type,
                    job_id,
                    self._org_id,
                    exc_info=True,
                )

    def _load_baseline(self, job_type: str, job_id: str) -> dict:
        """Totals a previous run of this job already wrote (pgmq redelivery), else zeros."""
        zero = {"tokens": 0, "cost_usd": Decimal(0), "calls": 0, "error_calls": 0}
        try:
            res = (
                self._client.table("llm_job_costs")
                .select("tokens,cost_usd,calls,error_calls")
                .eq("org_id", self._org_id)
                .eq("job_type", job_type)
                .eq("job_id", job_id)
                .execute()
            )
        except Exception:
            logger.warning("llm cost recorder: baseline read failed for %s %s", job_type, job_id, exc_info=True)
            return zero
        if not res.data:
            return zero
        r = res.data[0]
        return {
            "tokens": int(r["tokens"]),
            "cost_usd": Decimal(str(r["cost_usd"])),
            "calls": int(r["calls"]),
            "error_calls": int(r["error_calls"]),
        }

    def close(self) -> None:
        """Flush and detach from the exit hook. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self.flush()
        _OPEN_RECORDERS.discard(self)


_OPEN_RECORDERS: "weakref.WeakSet[LLMCostRecorder]" = weakref.WeakSet()


@atexit.register
def _flush_open_recorders() -> None:
    for recorder in list(_OPEN_RECORDERS):
        recorder.flush()


class _MeteredCompletions:
    def __init__(self, owner: "MeteredLLMClient"):
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self._owner._create(**kwargs)


class _MeteredChat:
    def __init__(self, owner: "MeteredLLMClient"):
        self.completions = _MeteredCompletions(owner)


class MeteredLLMClient:
    """Same `.chat.completions.create` shape as the wrapped client, plus recording.

    Every call is timed, its usage and estimated cost recorded, and any exception
    re-raised unchanged after an error row (class name only) is recorded. Recording
    can never alter the result or raise.
    """

    def __init__(self, base: LLMClient, ctx: CallContext, recorder: LLMCostRecorder):
        self._base = base
        self._ctx = ctx
        self._recorder = recorder
        self.chat = _MeteredChat(self)

    @property
    def context(self) -> CallContext:
        return self._ctx

    def bind(self, **fields: Any) -> "MeteredLLMClient":
        """A new client sharing the base client and recorder, with `fields` set on its
        context (`stage`, `purpose`, `entity_id`, `cluster_id`, `attempt`, `round`,
        `retry_reason`). The original is never modified, so a pool thread can bind
        freely without affecting its siblings."""
        unknown = set(fields) - _BINDABLE
        if unknown:
            raise TypeError(f"cannot bind {sorted(unknown)}; allowed: {sorted(_BINDABLE)}")
        return MeteredLLMClient(self._base, dataclasses.replace(self._ctx, **fields), self._recorder)

    def _create(self, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            response = self._base.chat.completions.create(**kwargs)
        except Exception as exc:
            self._record(kwargs, None, start, error=exc)
            raise
        self._record(kwargs, response, start, error=None)
        return response

    def _record(self, kwargs: dict, response: Any, start: float, error: Exception | None) -> None:
        try:
            latency_ms = int((time.perf_counter() - start) * 1000)
            deployment = kwargs.get("model")
            usage = _usage_fields(response) if response is not None else {}
            response_model = getattr(response, "model", None) if response is not None else None
            cost, pricing_version = (None, None)
            if error is None:
                cost, pricing_version = price_call(
                    response_model,
                    deployment,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_prompt_tokens=usage.get("cached_prompt_tokens"),
                )
            c = self._ctx
            self._recorder.record(
                {
                    "agent": c.agent,
                    "job_type": c.job_type,
                    "job_id": c.job_id,
                    "source_job_id": c.source_job_id,
                    "requested_by": c.requested_by,
                    "run_id": c.run_id,
                    "stage": c.stage,
                    "purpose": c.purpose,
                    "entity_id": c.entity_id,
                    "cluster_id": c.cluster_id,
                    "attempt": c.attempt,
                    "round": c.round,
                    "retry_reason": c.retry_reason,
                    "deployment": deployment,
                    "response_model": response_model,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_prompt_tokens": usage.get("cached_prompt_tokens"),
                    "reasoning_tokens": usage.get("reasoning_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "cost_usd": cost,
                    "pricing_version": pricing_version,
                    "latency_ms": latency_ms,
                    "status": "ok" if error is None else "error",
                    "error_class": type(error).__name__ if error is not None else None,
                }
            )
        except Exception:
            logger.warning("metered llm client: could not record a call", exc_info=True)


def bind_if_metered(client: Any, **fields: Any) -> Any:
    """`client.bind(**fields)` for a `MeteredLLMClient`; any other client (a test fake)
    passes through unchanged, so call sites never need to know which they were given."""
    return client.bind(**fields) if isinstance(client, MeteredLLMClient) else client


class _AttemptNumbered:
    """Numbers each call made through it as `attempt` 1, 2, ... and tags every call after
    the first `retry_reason='grounding'`. `check_grounding_with_retry` calls its grounding
    check once per attempt, so one instance per call chain records which grounding-retry
    round each call belonged to. Not shared across threads."""

    def __init__(self, base: MeteredLLMClient):
        self._base = base
        self._attempt = 0
        self.chat = self

    @property
    def completions(self) -> "_AttemptNumbered":
        return self

    def create(self, **kwargs: Any) -> Any:
        self._attempt += 1
        retry_reason = "grounding" if self._attempt > 1 else None
        bound = self._base.bind(attempt=self._attempt, retry_reason=retry_reason)
        return bound.chat.completions.create(**kwargs)


def attempt_numbered(client: Any) -> Any:
    """Wrap a `MeteredLLMClient` so successive calls are numbered as attempts; any other
    client passes through unchanged."""
    return _AttemptNumbered(client) if isinstance(client, MeteredLLMClient) else client
