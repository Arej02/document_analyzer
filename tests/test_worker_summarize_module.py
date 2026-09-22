import json

import pytest

from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import (
    ModuleEntitySummary,
    ModuleSummary,
    compute_module_risk,
    summarize_module,
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


def _entity_summary(name="process_payment", entity_type="function", risk_level="low", summary_text="Does a thing."):
    return ModuleEntitySummary(
        entity_name=name,
        entity_type=entity_type,
        summary=EntitySummary(summary_text=summary_text, risk_level=risk_level, risk_factors=[]),
    )


def test_summarize_module_sends_only_entity_name_type_and_summary_text():
    client = _FakeAzureClient(json.dumps({"summary": "Handles checkout."}))
    entities = [
        _entity_summary(name="process_payment", entity_type="function", risk_level="medium", summary_text="Charges via an external API."),
        _entity_summary(name="Cart", entity_type="class", risk_level="low", summary_text="Holds line items."),
    ]

    summarize_module("includes/checkout.php", entities, client=client)

    [call] = client.chat.completions.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    payload = json.loads(user_message["content"])
    assert payload == {
        "file_path": "includes/checkout.php",
        "entities": [
            {"name": "process_payment", "entity_type": "function", "summary": "Charges via an external API."},
            {"name": "Cart", "entity_type": "class", "summary": "Holds line items."},
        ],
    }
    assert call["model"] == "gpt-5.6-luna"


def test_summarize_module_returns_summary_text_from_llm():
    client = _FakeAzureClient(json.dumps({"summary": "This module handles checkout and payment."}))

    result = summarize_module("includes/checkout.php", [_entity_summary()], client=client)

    assert result.summary_text == "This module handles checkout and payment."


def test_summarize_module_risk_level_is_max_of_entities_not_llm_supplied():
    client = _FakeAzureClient(json.dumps({"summary": "Handles checkout.", "risk_level": "low"}))
    entities = [
        _entity_summary(name="process_payment", risk_level="high"),
        _entity_summary(name="get_total", risk_level="low"),
    ]

    result = summarize_module("includes/checkout.php", entities, client=client)

    assert result == ModuleSummary(
        summary_text="Handles checkout.",
        risk_level="high",
        risk_factors=["process_payment"],
    )


def test_summarize_module_raises_on_empty_entity_list():
    with pytest.raises(ValueError):
        summarize_module("includes/checkout.php", [], client=_FakeAzureClient("{}"))


def test_summarize_module_raises_on_malformed_json():
    client = _FakeAzureClient("not json")

    from document_analyzer.worker.summarize import SummarizationError

    with pytest.raises(SummarizationError):
        summarize_module("includes/checkout.php", [_entity_summary()], client=client)


def test_summarize_module_raises_on_missing_summary():
    client = _FakeAzureClient(json.dumps({}))

    from document_analyzer.worker.summarize import SummarizationError

    with pytest.raises(SummarizationError):
        summarize_module("includes/checkout.php", [_entity_summary()], client=client)


def test_compute_module_risk_empty_list_defaults_to_low():
    assert compute_module_risk([]) == ("low", [])


def test_compute_module_risk_all_low():
    entities = [_entity_summary(name="a", risk_level="low"), _entity_summary(name="b", risk_level="low")]

    assert compute_module_risk(entities) == ("low", ["a", "b"])


def test_compute_module_risk_mixed_levels_picks_highest_and_names_its_entities():
    entities = [
        _entity_summary(name="a", risk_level="low"),
        _entity_summary(name="b", risk_level="medium"),
        _entity_summary(name="c", risk_level="medium"),
    ]

    assert compute_module_risk(entities) == ("medium", ["b", "c"])
