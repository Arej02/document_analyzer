import json
from dataclasses import dataclass

import pytest

from document_analyzer.worker.ground_retry import (
    GROUNDING_RETRY_CAP,
    GroundingRetryResult,
    check_grounding_with_retry,
)


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


class _SequencedCompletions:
    """Returns one canned grounding response per call, in order."""

    def __init__(self, contents):
        self._contents = list(contents)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._contents.pop(0))


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAzureClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


@dataclass(frozen=True)
class _StubSummary:
    summary_text: str
    risk_level: str
    risk_factors: list[str]


def _grounded(issues=None):
    return json.dumps({"grounded": True, "issues": issues or []})


def _ungrounded(issues=None):
    return json.dumps({"grounded": False, "issues": issues or ["invented a claim"]})


class _CountingGenerate:
    def __init__(self, summaries):
        self._summaries = list(summaries)
        self.calls = 0

    def __call__(self):
        summary = self._summaries[self.calls]
        self.calls += 1
        return summary


_FACTS = {"entity_type": "function", "name": "process_payment"}


def test_returns_immediately_when_first_attempt_is_grounded():
    client = _FakeAzureClient(_SequencedCompletions([_grounded()]))
    generate = _CountingGenerate([_StubSummary("Does a thing.", "low", [])])

    result = check_grounding_with_retry(_FACTS, generate, client=client)

    assert generate.calls == 1
    assert result == GroundingRetryResult(
        summary=_StubSummary("Does a thing.", "low", []),
        grounding=result.grounding,
        retries_used=0,
    )
    assert result.grounding.verdict == "grounded"


def test_retries_until_grounded_within_cap():
    client = _FakeAzureClient(_SequencedCompletions([_ungrounded(), _grounded()]))
    generate = _CountingGenerate(
        [_StubSummary("Bad attempt.", "low", []), _StubSummary("Good attempt.", "medium", ["outbound_network_call"])]
    )

    result = check_grounding_with_retry(_FACTS, generate, client=client)

    assert generate.calls == 2
    assert result.retries_used == 1
    assert result.summary == _StubSummary("Good attempt.", "medium", ["outbound_network_call"])
    assert result.grounding.verdict == "grounded"


def test_stops_at_cap_and_returns_last_ungrounded_result():
    client = _FakeAzureClient(_SequencedCompletions([_ungrounded(["a"]), _ungrounded(["b"]), _ungrounded(["c"])]))
    generate = _CountingGenerate(
        [_StubSummary("Attempt 1.", "low", []), _StubSummary("Attempt 2.", "low", []), _StubSummary("Attempt 3.", "low", [])]
    )

    result = check_grounding_with_retry(_FACTS, generate, max_retries=2, client=client)

    assert generate.calls == 3
    assert result.retries_used == 2
    assert result.grounding.verdict == "ungrounded"
    assert result.grounding.issues == ["c"]
    assert result.summary == _StubSummary("Attempt 3.", "low", [])


def test_max_retries_overrides_default_cap():
    client = _FakeAzureClient(_SequencedCompletions([_ungrounded(), _ungrounded()]))
    generate = _CountingGenerate([_StubSummary("A.", "low", []), _StubSummary("B.", "low", [])])

    result = check_grounding_with_retry(_FACTS, generate, max_retries=1, client=client)

    assert generate.calls == 2
    assert result.retries_used == 1
    assert result.grounding.verdict == "ungrounded"


def test_zero_max_retries_never_regenerates():
    client = _FakeAzureClient(_SequencedCompletions([_ungrounded()]))
    generate = _CountingGenerate([_StubSummary("Only attempt.", "low", [])])

    result = check_grounding_with_retry(_FACTS, generate, max_retries=0, client=client)

    assert generate.calls == 1
    assert result.retries_used == 0
    assert result.grounding.verdict == "ungrounded"


def test_default_grounding_retry_cap_is_two():
    assert GROUNDING_RETRY_CAP == 2
