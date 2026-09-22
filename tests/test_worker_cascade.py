import json

import pytest

from document_analyzer.worker.cascade import CascadeResult, regenerate_ancestors
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleEntitySummary
from document_analyzer.worker.summarize_subsystem import ChildSummary


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
    """Returns one canned response per call, in order, recording each call's payload."""

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


def _payload(call):
    return json.loads(next(m for m in call["messages"] if m["role"] == "user")["content"])


def _module_entity_summaries():
    return [
        ModuleEntitySummary(
            entity_name="process_payment",
            entity_type="function",
            summary=EntitySummary(summary_text="Charges the customer.", risk_level="high", risk_factors=["outbound_network_call"]),
        )
    ]


def test_regenerates_module_subsystem_and_repo_in_that_order():
    client = _FakeAzureClient(
        _SequencedCompletions(
            [
                json.dumps({"summary": "Module: handles checkout."}),
                json.dumps({"summary": "Subsystem: includes handles core site logic."}),
                json.dumps({"summary": "Repo: an e-commerce WordPress plugin."}),
            ]
        )
    )

    result = regenerate_ancestors(
        "includes/checkout.php",
        _module_entity_summaries(),
        sibling_module_summaries=[],
        sibling_subsystem_summaries=[],
        client=client,
    )

    assert isinstance(result, CascadeResult)
    assert result.file_path == "includes/checkout.php"
    assert result.subsystem_name == "includes"
    assert result.module_summary.summary_text == "Module: handles checkout."
    assert result.subsystem_summary.summary_text == "Subsystem: includes handles core site logic."
    assert result.repo_summary.summary_text == "Repo: an e-commerce WordPress plugin."
    assert len(client.chat.completions.calls) == 3


def test_module_regeneration_uses_the_full_given_entity_set():
    client = _FakeAzureClient(
        _SequencedCompletions(
            [
                json.dumps({"summary": "Module summary."}),
                json.dumps({"summary": "Subsystem summary."}),
                json.dumps({"summary": "Repo summary."}),
            ]
        )
    )
    entity_summaries = _module_entity_summaries()

    regenerate_ancestors("includes/checkout.php", entity_summaries, [], [], client=client)

    module_call_payload = _payload(client.chat.completions.calls[0])
    assert module_call_payload == {
        "file_path": "includes/checkout.php",
        "entities": [{"name": "process_payment", "entity_type": "function", "summary": "Charges the customer."}],
    }


def test_subsystem_regeneration_folds_the_fresh_module_alongside_siblings():
    client = _FakeAzureClient(
        _SequencedCompletions(
            [
                json.dumps({"summary": "Fresh checkout module summary."}),
                json.dumps({"summary": "Subsystem summary."}),
                json.dumps({"summary": "Repo summary."}),
            ]
        )
    )
    sibling_module = ChildSummary(name="includes/cart.php", summary_text="Handles the cart.", risk_level="low")

    regenerate_ancestors(
        "includes/checkout.php", _module_entity_summaries(), sibling_module_summaries=[sibling_module], sibling_subsystem_summaries=[], client=client
    )

    subsystem_call_payload = _payload(client.chat.completions.calls[1])
    assert subsystem_call_payload == {
        "name": "includes",
        "children": [
            {"name": "includes/checkout.php", "summary": "Fresh checkout module summary."},
            {"name": "includes/cart.php", "summary": "Handles the cart."},
        ],
    }


def test_repo_regeneration_folds_the_fresh_subsystem_alongside_siblings():
    client = _FakeAzureClient(
        _SequencedCompletions(
            [
                json.dumps({"summary": "Module summary."}),
                json.dumps({"summary": "Fresh includes subsystem summary."}),
                json.dumps({"summary": "Repo summary."}),
            ]
        )
    )
    sibling_subsystem = ChildSummary(name="admin", summary_text="Handles admin screens.", risk_level="low")

    regenerate_ancestors(
        "includes/checkout.php",
        _module_entity_summaries(),
        sibling_module_summaries=[],
        sibling_subsystem_summaries=[sibling_subsystem],
        client=client,
    )

    repo_call_payload = _payload(client.chat.completions.calls[2])
    assert repo_call_payload == {
        "name": "repo",
        "children": [
            {"name": "includes", "summary": "Fresh includes subsystem summary."},
            {"name": "admin", "summary": "Handles admin screens."},
        ],
    }


def test_subsystem_name_computed_from_file_path():
    client = _FakeAzureClient(
        _SequencedCompletions(
            [json.dumps({"summary": "Module summary."}), json.dumps({"summary": "Subsystem summary."}), json.dumps({"summary": "Repo summary."})]
        )
    )

    result = regenerate_ancestors("index.php", _module_entity_summaries(), [], [], client=client)

    assert result.subsystem_name == "(root)"
