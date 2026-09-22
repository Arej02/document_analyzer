import json

import pytest

from document_analyzer.worker.ground import GroundingCheckError, GroundingResult, check_grounding


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


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeAzureClient:
    def __init__(self, content):
        self.chat = _FakeChat(content)


_FACTS = {"entity_type": "function", "name": "process_payment", "raw_facts": {"calls": ["curl_exec"]}}


def test_check_grounding_sends_facts_and_generated_summary():
    client = _FakeAzureClient(json.dumps({"grounded": True, "issues": []}))

    check_grounding(_FACTS, "Charges the customer via an external API.", "high", ["outbound_network_call"], client=client)

    [call] = client.chat.completions.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    payload = json.loads(user_message["content"])
    assert payload == {
        "facts": _FACTS,
        "generated_summary": {
            "summary": "Charges the customer via an external API.",
            "risk_level": "high",
            "risk_factors": ["outbound_network_call"],
        },
    }
    assert call["model"] == "gpt-5.6-luna"


def test_check_grounding_returns_grounded_verdict():
    client = _FakeAzureClient(json.dumps({"grounded": True, "issues": []}))

    result = check_grounding(_FACTS, "Charges the customer.", "high", ["outbound_network_call"], client=client)

    assert result == GroundingResult(verdict="grounded", issues=[])


def test_check_grounding_returns_ungrounded_verdict_with_issues():
    client = _FakeAzureClient(
        json.dumps({"grounded": False, "issues": ["summary invents a discount calculation not present in facts"]})
    )

    result = check_grounding(_FACTS, "Charges the customer and applies a loyalty discount.", "low", [], client=client)

    assert result == GroundingResult(
        verdict="ungrounded", issues=["summary invents a discount calculation not present in facts"]
    )


def test_check_grounding_defaults_missing_issues_to_empty_list():
    client = _FakeAzureClient(json.dumps({"grounded": True}))

    result = check_grounding(_FACTS, "Charges the customer.", "high", [], client=client)

    assert result.issues == []


def test_check_grounding_raises_on_malformed_json():
    client = _FakeAzureClient("not json")

    with pytest.raises(GroundingCheckError):
        check_grounding(_FACTS, "Charges the customer.", "high", [], client=client)


def test_check_grounding_raises_on_missing_grounded_field():
    client = _FakeAzureClient(json.dumps({"issues": []}))

    with pytest.raises(GroundingCheckError):
        check_grounding(_FACTS, "Charges the customer.", "high", [], client=client)


def test_check_grounding_raises_on_non_bool_grounded():
    client = _FakeAzureClient(json.dumps({"grounded": "yes", "issues": []}))

    with pytest.raises(GroundingCheckError):
        check_grounding(_FACTS, "Charges the customer.", "high", [], client=client)


def test_check_grounding_raises_on_non_list_issues():
    client = _FakeAzureClient(json.dumps({"grounded": False, "issues": "invented a discount"}))

    with pytest.raises(GroundingCheckError):
        check_grounding(_FACTS, "Charges the customer.", "high", [], client=client)
