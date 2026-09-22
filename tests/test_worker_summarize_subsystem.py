import json

import pytest

from document_analyzer.worker.summarize import SummarizationError
from document_analyzer.worker.summarize_subsystem import (
    ChildSummary,
    RepoSummary,
    SubsystemSummary,
    subsystem_for_file,
    summarize_repo,
    summarize_subsystem,
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


def _child(name="checkout.php", summary_text="Handles checkout.", risk_level="low"):
    return ChildSummary(name=name, summary_text=summary_text, risk_level=risk_level)


# --- subsystem_for_file --------------------------------------------------


def test_subsystem_for_file_uses_top_level_directory():
    assert subsystem_for_file("includes/checkout.php") == "includes"


def test_subsystem_for_file_uses_top_level_directory_for_deeply_nested_paths():
    assert subsystem_for_file("wp-content/plugins/foo/includes/checkout.php") == "wp-content"


def test_subsystem_for_file_root_file_gets_sentinel():
    assert subsystem_for_file("index.php") == "(root)"


# --- summarize_subsystem --------------------------------------------------


def test_summarize_subsystem_sends_only_module_name_and_summary_text():
    client = _FakeAzureClient(json.dumps({"summary": "Handles checkout and cart."}))
    modules = [
        _child(name="checkout.php", summary_text="Charges the customer.", risk_level="medium"),
        _child(name="cart.php", summary_text="Holds line items.", risk_level="low"),
    ]

    summarize_subsystem("includes", modules, client=client)

    [call] = client.chat.completions.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    payload = json.loads(user_message["content"])
    assert payload == {
        "name": "includes",
        "children": [
            {"name": "checkout.php", "summary": "Charges the customer."},
            {"name": "cart.php", "summary": "Holds line items."},
        ],
    }
    assert call["model"] == "gpt-5.6-luna"


def test_summarize_subsystem_risk_level_is_max_of_modules_not_llm_supplied():
    client = _FakeAzureClient(json.dumps({"summary": "Handles checkout.", "risk_level": "low"}))
    modules = [
        _child(name="checkout.php", risk_level="high"),
        _child(name="cart.php", risk_level="low"),
    ]

    result = summarize_subsystem("includes", modules, client=client)

    assert result == SubsystemSummary(
        summary_text="Handles checkout.",
        risk_level="high",
        risk_factors=["checkout.php"],
    )


def test_summarize_subsystem_raises_on_empty_module_list():
    with pytest.raises(ValueError):
        summarize_subsystem("includes", [], client=_FakeAzureClient("{}"))


def test_summarize_subsystem_raises_on_malformed_json():
    client = _FakeAzureClient("not json")

    with pytest.raises(SummarizationError):
        summarize_subsystem("includes", [_child()], client=client)


def test_summarize_subsystem_raises_on_missing_summary():
    client = _FakeAzureClient(json.dumps({}))

    with pytest.raises(SummarizationError):
        summarize_subsystem("includes", [_child()], client=client)


# --- summarize_repo --------------------------------------------------


def test_summarize_repo_sends_only_subsystem_name_and_summary_text():
    client = _FakeAzureClient(json.dumps({"summary": "A WordPress plugin for e-commerce."}))
    subsystems = [
        _child(name="includes", summary_text="Handles checkout and cart.", risk_level="medium"),
        _child(name="admin", summary_text="Renders the settings page.", risk_level="low"),
    ]

    summarize_repo(subsystems, client=client)

    [call] = client.chat.completions.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    payload = json.loads(user_message["content"])
    assert payload == {
        "name": "repo",
        "children": [
            {"name": "includes", "summary": "Handles checkout and cart."},
            {"name": "admin", "summary": "Renders the settings page."},
        ],
    }


def test_summarize_repo_risk_level_is_max_of_subsystems():
    client = _FakeAzureClient(json.dumps({"summary": "A WordPress plugin."}))
    subsystems = [
        _child(name="includes", risk_level="medium"),
        _child(name="admin", risk_level="high"),
    ]

    result = summarize_repo(subsystems, client=client)

    assert result == RepoSummary(
        summary_text="A WordPress plugin.",
        risk_level="high",
        risk_factors=["admin"],
    )


def test_summarize_repo_raises_on_empty_subsystem_list():
    with pytest.raises(ValueError):
        summarize_repo([], client=_FakeAzureClient("{}"))


def test_summarize_repo_raises_on_malformed_json():
    client = _FakeAzureClient("not json")

    with pytest.raises(SummarizationError):
        summarize_repo([_child()], client=client)
