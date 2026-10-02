"""shared/llm.py — MeteredLLMClient and LLMCostRecorder (design.md section 7.3)."""

import datetime as dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from document_analyzer.shared import llm, llm_pricing
from document_analyzer.shared.llm import CallContext, LLMCostRecorder, MeteredLLMClient, new_run_id

PRICES = (llm_pricing.PriceEntry("t1", "gpt-x", dt.date(2026, 1, 1), Decimal(2), Decimal(1), Decimal(8)),)


@pytest.fixture(autouse=True)
def _prices(monkeypatch):
    monkeypatch.setattr(llm_pricing, "load_price_table", lambda *a, **k: PRICES)


class CostDB:
    """Just enough of a Supabase client for the recorder: insert (one batch per call), upsert and select."""

    def __init__(self, fail_inserts=False):
        self.tables: dict[str, list[dict]] = {}
        self.fail_inserts = fail_inserts
        self.insert_batches = 0

    def table(self, name):
        return _Table(self, name)


class _Table:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self._filters, self._pending = {}, None

    def insert(self, rows):
        self._pending = ("insert", rows)
        return self

    def upsert(self, row, on_conflict=None):
        self._pending = ("upsert", row, on_conflict)
        return self

    def select(self, *_):
        self._pending = ("select",)
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        kind = self._pending[0]
        if kind == "insert":
            if self.db.fail_inserts:
                raise RuntimeError("database unavailable")
            self.db.insert_batches += 1
            rows.extend(self._pending[1])
            return NS(data=self._pending[1])
        if kind == "upsert":
            row, keys = self._pending[1], self._pending[2].split(",")
            for existing in rows:
                if all(existing[k] == row[k] for k in keys):
                    existing.update(row)
                    break
            else:
                rows.append(dict(row))
            return NS(data=[row])
        return NS(data=[r for r in rows if all(r.get(k) == v for k, v in self._filters.items())])


def reply(model="gpt-x-2026", prompt=100, completion=50, cached=20, reasoning=5):
    return NS(
        model=model,
        choices=[NS(message=NS(content="{}"))],
        usage=NS(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=NS(cached_tokens=cached),
            completion_tokens_details=NS(reasoning_tokens=reasoning),
        ),
    )


class FakeBase:
    def __init__(self, response=None, error=None):
        self.calls = []
        outer = self

        def create(**kwargs):
            outer.calls.append(kwargs)
            if error:
                raise error
            return response or reply()

        self.chat = NS(completions=NS(create=create))


def make(base=None, db=None, ctx=None, **recorder_kwargs):
    db = db or CostDB()
    recorder = LLMCostRecorder(db, "org-1", **recorder_kwargs)
    ctx = ctx or CallContext("agent1", "ingestion", "job-1", new_run_id())
    return MeteredLLMClient(base or FakeBase(), ctx, recorder), recorder, db


def rows(db):
    return db.tables.get("llm_invocations", [])


# --- recording a call ---------------------------------------------------------------------------


def test_a_successful_call_is_recorded_with_usage_cost_and_latency():
    client, recorder, db = make()

    response = client.bind(stage="summarize", purpose="entity_summary", entity_id="e1").chat.completions.create(
        model="my-deployment", messages=[]
    )
    recorder.flush()

    assert response.choices[0].message.content == "{}"  # the caller still gets the real response
    [row] = rows(db)
    assert (row["agent"], row["job_type"], row["job_id"], row["org_id"]) == ("agent1", "ingestion", "job-1", "org-1")
    assert (row["stage"], row["purpose"], row["entity_id"]) == ("summarize", "entity_summary", "e1")
    assert (row["deployment"], row["response_model"]) == ("my-deployment", "gpt-x-2026")
    assert (row["prompt_tokens"], row["completion_tokens"], row["cached_prompt_tokens"]) == (100, 50, 20)
    assert (row["reasoning_tokens"], row["total_tokens"]) == (5, 150)
    # 80 uncached * 2 + 20 cached * 1 + 50 completion * 8 = 580 per million
    assert row["cost_usd"] == Decimal("0.000580") or row["cost_usd"] == "0.000580"
    assert row["pricing_version"] == "t1"
    assert row["status"] == "ok" and row["error_class"] is None
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0


def test_prompts_and_responses_are_never_recorded():
    client, recorder, db = make()
    client.chat.completions.create(model="d", messages=[{"role": "user", "content": "SECRET PROMPT"}])
    recorder.flush()
    assert "SECRET PROMPT" not in repr(rows(db)) and "messages" not in rows(db)[0]


def test_an_unknown_model_records_null_cost_and_does_not_fail_the_call():
    client, recorder, db = make(FakeBase(reply(model="mystery")))
    client.chat.completions.create(model="d")
    recorder.flush()
    assert rows(db)[0]["cost_usd"] is None and rows(db)[0]["pricing_version"] is None


def test_missing_usage_is_recorded_as_unknown_tokens():
    client, recorder, db = make(FakeBase(NS(model="gpt-x", choices=[], usage=None)))
    client.chat.completions.create(model="d")
    recorder.flush()
    row = rows(db)[0]
    assert row["prompt_tokens"] is None and row["total_tokens"] is None and row["cost_usd"] is None


def test_an_llm_error_is_recorded_by_class_name_and_the_original_exception_is_reraised():
    boom = TimeoutError("sk-secret-in-the-message")
    client, recorder, db = make(FakeBase(error=boom))

    with pytest.raises(TimeoutError) as caught:
        client.chat.completions.create(model="d")
    recorder.flush()

    assert caught.value is boom
    [row] = rows(db)
    assert row["status"] == "error" and row["error_class"] == "TimeoutError" and row["cost_usd"] is None
    assert "sk-secret" not in repr(row)


# --- never blocks work ---------------------------------------------------------------------------


def test_a_failing_database_never_breaks_the_call_or_the_flush(caplog):
    caplog.set_level(logging.WARNING)
    client, recorder, db = make(db=CostDB(fail_inserts=True))

    response = client.chat.completions.create(model="d")  # must not raise
    recorder.flush()  # must not raise
    recorder.close()  # must not raise

    assert response is not None
    assert any("dropped 1 llm_invocations row" in r.getMessage() for r in caplog.records)


def test_a_failure_while_building_the_row_never_breaks_the_call(monkeypatch):
    client, recorder, _ = make()
    monkeypatch.setattr(llm, "price_call", lambda *a, **k: 1 / 0)
    assert client.chat.completions.create(model="d") is not None


def test_a_recorder_that_raises_on_record_never_breaks_the_call():
    class Exploding:
        def record(self, row):
            raise RuntimeError("boom")

    client = MeteredLLMClient(FakeBase(), CallContext("agent1", "ingestion", "j", "r"), Exploding())
    assert client.chat.completions.create(model="d") is not None


# --- flushing --------------------------------------------------------------------------------------


def test_rows_are_buffered_until_the_batch_size_then_flushed():
    client, recorder, db = make(batch_size=3, flush_interval=3600)
    for _ in range(2):
        client.chat.completions.create(model="d")
    assert rows(db) == []
    client.chat.completions.create(model="d")
    assert len(rows(db)) == 3 and db.insert_batches == 1


def test_rows_are_flushed_after_the_interval(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    client, recorder, db = make(batch_size=1000, flush_interval=5)
    client.chat.completions.create(model="d")
    assert rows(db) == []
    now[0] += 6
    client.chat.completions.create(model="d")
    assert len(rows(db)) == 2


def test_close_flushes_what_is_left_and_is_idempotent():
    client, recorder, db = make(batch_size=1000)
    client.chat.completions.create(model="d")
    recorder.close()
    recorder.close()
    assert len(rows(db)) == 1 and db.insert_batches == 1


def test_the_exit_hook_flushes_recorders_that_were_never_closed():
    client, recorder, db = make(batch_size=1000)
    client.chat.completions.create(model="d")
    llm._flush_open_recorders()
    assert len(rows(db)) == 1


def test_the_rollup_is_upserted_from_in_memory_totals_and_never_for_qa():
    client, recorder, db = make(batch_size=1000)
    client.chat.completions.create(model="d")
    client.chat.completions.create(model="d")
    recorder.flush()
    [rollup] = db.tables["llm_job_costs"]
    assert (rollup["agent"], rollup["job_type"], rollup["job_id"], rollup["calls"]) == ("agent1", "ingestion", "job-1", 2)
    assert rollup["tokens"] == 300 and Decimal(rollup["cost_usd"]) == Decimal("0.001160")

    qa_db = CostDB()
    qa_client, qa_recorder, _ = make(db=qa_db, ctx=CallContext("agent1", "qa", None, new_run_id(), source_job_id="job-1"))
    qa_client.chat.completions.create(model="d")
    qa_recorder.flush()
    assert "llm_job_costs" not in qa_db.tables and len(rows(qa_db)) == 1


# --- context ----------------------------------------------------------------------------------------


def test_bind_returns_a_new_client_and_never_modifies_the_original():
    client, recorder, db = make()
    bound = client.bind(purpose="entity_summary", attempt=2, retry_reason="grounding", cluster_id="c1", round=3)

    assert bound is not client
    assert client.context.purpose == "unspecified" and client.context.attempt is None
    assert (bound.context.purpose, bound.context.attempt, bound.context.retry_reason) == ("entity_summary", 2, "grounding")
    assert (bound.context.cluster_id, bound.context.round) == ("c1", 3)

    client.chat.completions.create(model="d")
    bound.chat.completions.create(model="d")
    recorder.flush()
    assert [r["purpose"] for r in rows(db)] == ["unspecified", "entity_summary"]


def test_bind_rejects_fields_that_are_not_part_of_the_call_context():
    client, _, _ = make()
    for forbidden in ("org_id", "job_id", "agent", "run_id"):
        with pytest.raises(TypeError):
            client.bind(**{forbidden: "x"})


def test_org_id_comes_only_from_the_recorder_never_from_the_row_or_context():
    client, recorder, db = make()
    assert not hasattr(client.context, "org_id")
    client.chat.completions.create(model="d")
    recorder.flush()
    assert rows(db)[0]["org_id"] == "org-1"


def test_the_context_enforces_the_agent_to_job_type_pairing_like_the_table_check():
    with pytest.raises(ValueError):
        CallContext("agent2", "ingestion", "j")
    with pytest.raises(ValueError):
        CallContext("agent1", "qa", "has-a-job-id")
    with pytest.raises(ValueError):
        CallContext("agent1", "ingestion", None)
    CallContext("agent3", "execution", "j")


def test_each_pickup_gets_its_own_run_id_so_a_redelivered_job_stays_separable():
    assert len({new_run_id() for _ in range(100)}) == 100

    db = CostDB()
    for _ in range(2):  # the same job picked up twice, as after a pgmq redelivery
        client, recorder, _ = make(db=db, ctx=CallContext("agent2", "planning", "pj-1", new_run_id()))
        client.chat.completions.create(model="d")
        recorder.close()

    first, second = rows(db)
    assert first["job_id"] == second["job_id"] == "pj-1" and first["run_id"] != second["run_id"]
    # ...and the rollup adds the second run to what the first already wrote instead of overwriting it.
    [rollup] = db.tables["llm_job_costs"]
    assert rollup["calls"] == 2 and rollup["tokens"] == 300


# --- concurrency ------------------------------------------------------------------------------------


def test_a_thread_pool_loses_no_rows_and_no_totals():
    client, recorder, db = make(batch_size=7, flush_interval=3600)
    barrier = threading.Barrier(8)

    def work(i):
        barrier.wait()
        bound = client.bind(purpose="entity_summary", entity_id=f"e{i}")
        for _ in range(25):
            bound.chat.completions.create(model="d")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    recorder.close()

    assert len(rows(db)) == 200
    assert sorted({r["entity_id"] for r in rows(db)}) == sorted(f"e{i}" for i in range(8))
    [rollup] = db.tables["llm_job_costs"]
    assert rollup["calls"] == 200 and rollup["tokens"] == 200 * 150
