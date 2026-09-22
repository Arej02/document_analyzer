import json

import pytest

from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.summarize import (
    EntitySummary,
    SummarizationError,
    get_azure_openai_client,
    summarize_entity,
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


def _entity(raw_facts=None):
    return ExtractedEntity(
        local_id="fn:includes/checkout.php:process_payment",
        entity_type="function",
        name="process_payment",
        file_path="includes/checkout.php",
        raw_facts=raw_facts if raw_facts is not None else {"params": ["order_id"], "calls": ["curl_exec"]},
    )


def test_summarize_entity_sends_only_this_entitys_own_facts():
    client = _FakeAzureClient(json.dumps({"summary": "Processes payment.", "risk_level": "medium", "risk_factors": ["outbound_network_call"]}))
    entity = _entity()

    summarize_entity(entity, client=client)

    [call] = client.chat.completions.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    payload = json.loads(user_message["content"])
    assert payload == {
        "entity_type": "function",
        "name": "process_payment",
        "file_path": "includes/checkout.php",
        "raw_facts": {"params": ["order_id"], "calls": ["curl_exec"]},
    }
    assert call["model"] == "gpt-5.6-luna"


def test_summarize_entity_returns_summary_and_risk_level():
    client = _FakeAzureClient(
        json.dumps({"summary": "Charges the customer via an external API.", "risk_level": "high", "risk_factors": ["outbound_network_call"]})
    )

    result = summarize_entity(_entity(), client=client)

    assert result == EntitySummary(
        summary_text="Charges the customer via an external API.",
        risk_level="high",
        risk_factors=["outbound_network_call"],
    )


def test_summarize_entity_defaults_missing_risk_factors_to_empty_list():
    client = _FakeAzureClient(json.dumps({"summary": "A getter.", "risk_level": "low"}))

    result = summarize_entity(_entity(), client=client)

    assert result.risk_factors == []


def test_summarize_entity_raises_on_malformed_json():
    client = _FakeAzureClient("not json")

    with pytest.raises(SummarizationError):
        summarize_entity(_entity(), client=client)


def test_summarize_entity_raises_on_invalid_risk_level():
    client = _FakeAzureClient(json.dumps({"summary": "Does a thing.", "risk_level": "critical"}))

    with pytest.raises(SummarizationError):
        summarize_entity(_entity(), client=client)


def test_summarize_entity_raises_on_missing_summary():
    client = _FakeAzureClient(json.dumps({"risk_level": "low"}))

    with pytest.raises(SummarizationError):
        summarize_entity(_entity(), client=client)


def test_get_azure_openai_client_uses_env_config():
    client = get_azure_openai_client()

    assert client.api_key == "fake-key"
    assert client._api_version == "2026-07-09"
    assert str(client.base_url).startswith("https://example.openai.azure.com")
