import json
import threading
import time

import pytest

from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.summarize import EntitySummary, SummarizationError
from document_analyzer.worker.summarize_fanout import summarize_entities


@pytest.fixture(autouse=True)
def azure_openai_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2026-07-09")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-luna")


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _entity(local_id, name):
    return ExtractedEntity(
        local_id=local_id,
        entity_type="function",
        name=name,
        file_path="includes/checkout.php",
        raw_facts={"params": []},
    )


class _OrderedCompletions:
    """Returns a distinct, per-entity-name response so ordering can be checked."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["messages"][1]["content"])
        summary = f"Summary of {payload['name']}."
        return _FakeResponse(json.dumps({"summary": summary, "risk_level": "low", "risk_factors": []}))


class _ConcurrencyTrackingCompletions:
    """Tracks how many create() calls are in flight at once, holding each one open
    briefly so overlapping calls actually overlap."""

    def __init__(self, hold_seconds=0.05):
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0
        self._hold_seconds = hold_seconds

    def create(self, **kwargs):
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        time.sleep(self._hold_seconds)
        with self._lock:
            self._current -= 1
        return _FakeResponse(json.dumps({"summary": "A thing.", "risk_level": "low", "risk_factors": []}))


class _FailingCompletions:
    def create(self, **kwargs):
        return _FakeResponse("not json")


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAzureClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


def test_summarize_entities_empty_list_returns_empty_list():
    assert summarize_entities([], client=_FakeAzureClient(_OrderedCompletions())) == []


def test_summarize_entities_returns_results_in_input_order():
    entities = [_entity(f"fn:{i}", f"fn_{i}") for i in range(5)]
    client = _FakeAzureClient(_OrderedCompletions())

    results = summarize_entities(entities, client=client, max_workers=3)

    assert results == [
        EntitySummary(summary_text=f"Summary of fn_{i}.", risk_level="low", risk_factors=[]) for i in range(5)
    ]


def test_summarize_entities_respects_max_workers_cap():
    entities = [_entity(f"fn:{i}", f"fn_{i}") for i in range(6)]
    completions = _ConcurrencyTrackingCompletions()
    client = _FakeAzureClient(completions)

    summarize_entities(entities, client=client, max_workers=3)

    assert completions.max_concurrent <= 3


def test_summarize_entities_actually_runs_concurrently_not_sequentially():
    entities = [_entity(f"fn:{i}", f"fn_{i}") for i in range(6)]
    completions = _ConcurrencyTrackingCompletions()
    client = _FakeAzureClient(completions)

    summarize_entities(entities, client=client, max_workers=3)

    assert completions.max_concurrent > 1


def test_summarize_entities_max_workers_overrides_default_cap():
    entities = [_entity(f"fn:{i}", f"fn_{i}") for i in range(10)]
    completions = _ConcurrencyTrackingCompletions()
    client = _FakeAzureClient(completions)

    summarize_entities(entities, client=client, max_workers=1)

    assert completions.max_concurrent == 1


def test_summarize_entities_propagates_a_single_entity_error():
    entities = [_entity("fn:0", "fn_0")]
    client = _FakeAzureClient(_FailingCompletions())

    with pytest.raises(SummarizationError):
        summarize_entities(entities, client=client, max_workers=2)


def test_default_summary_concurrency_is_ten():
    from document_analyzer.worker.summarize_fanout import SUMMARY_CONCURRENCY

    assert SUMMARY_CONCURRENCY == 10
