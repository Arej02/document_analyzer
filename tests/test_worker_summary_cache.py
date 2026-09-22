import json

import pytest

from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.summary_cache import (
    lookup_cached_summary,
    summarize_entity_cached,
    write_through_cache,
)

from fakes import FakeClient


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
    def __init__(self, completions):
        self.completions = completions


class _FakeAzureClient:
    def __init__(self, content):
        self.chat = _FakeChat(_FakeCompletions(content))


def _entity():
    return ExtractedEntity(
        local_id="function:app.php:do_thing:3",
        entity_type="function",
        name="do_thing",
        file_path="app.php",
        raw_facts={"params": ["a"]},
    )


def _fake_embed(_text):
    return [1.0, 2.0, 3.0]


# --- lookup_cached_summary / write_through_cache --------------------------------------------------


def test_lookup_cached_summary_returns_none_on_miss():
    client = FakeClient(org_id="org-123")

    assert lookup_cached_summary(client, "org-123", "hash-1") is None


def test_write_through_then_lookup_returns_the_summary_row():
    client = FakeClient(org_id="org-123")
    client.table("knowledge_summaries").rows.append(
        {"id": "summary-1", "org_id": "org-123", "summary_text": "Does a thing."}
    )

    write_through_cache(client, "org-123", "hash-1", "summary-1")
    cached = lookup_cached_summary(client, "org-123", "hash-1")

    assert cached == {"id": "summary-1", "org_id": "org-123", "summary_text": "Does a thing."}


def test_lookup_cached_summary_is_scoped_by_org_id():
    client = FakeClient(org_id="org-123")
    client.table("knowledge_summaries").rows.append(
        {"id": "summary-1", "org_id": "org-123", "summary_text": "Does a thing."}
    )
    write_through_cache(client, "org-123", "hash-1", "summary-1")

    assert lookup_cached_summary(client, "org-456", "hash-1") is None


# --- summarize_entity_cached --------------------------------------------------


def test_summarize_entity_cached_miss_calls_llm_persists_and_writes_through():
    llm_client = _FakeAzureClient(json.dumps({"summary": "Does a thing.", "risk_level": "low", "risk_factors": []}))
    client = FakeClient(org_id="org-123")

    row = summarize_entity_cached(
        client,
        job_id="job-1",
        org_id="org-123",
        entity=_entity(),
        entity_db_id="entity-1",
        content_hash="hash-1",
        llm_client=llm_client,
        embed=_fake_embed,
    )

    assert len(llm_client.chat.completions.calls) == 1
    assert row["summary_text"] == "Does a thing."
    assert row["entity_id"] == "entity-1"
    [cache_row] = client.table("summary_cache").rows
    assert cache_row["org_id"] == "org-123"
    assert cache_row["content_hash"] == "hash-1"
    assert cache_row["summary_id"] == row["id"]


def test_summarize_entity_cached_hit_skips_the_llm_call():
    llm_client = _FakeAzureClient(json.dumps({"summary": "Does a thing.", "risk_level": "low", "risk_factors": []}))
    client = FakeClient(org_id="org-123")
    client.table("knowledge_summaries").rows.append(
        {
            "id": "summary-cached",
            "org_id": "org-123",
            "summary_text": "Cached summary.",
            "risk_level": "low",
            "embedding": [1.0, 2.0, 3.0],
            "grounding_status": "verified",
            "entity_id": "entity-1",
            "job_id": "job-0",
        }
    )
    write_through_cache(client, "org-123", "hash-1", "summary-cached")

    row = summarize_entity_cached(
        client,
        job_id="job-1",
        org_id="org-123",
        entity=_entity(),
        entity_db_id="entity-1",
        content_hash="hash-1",
        llm_client=llm_client,
        embed=_fake_embed,
    )

    assert llm_client.chat.completions.calls == []
    assert row["summary_text"] == "Cached summary."
    assert row["grounding_status"] == "verified"
    # A new, job-scoped row — not the original cached one — so GET .../entities can
    # find it by (job_id, entity_id) for *this* job (tasks.md Phase 5's confirmation).
    assert row["id"] != "summary-cached"
    assert row["job_id"] == "job-1"
    assert row["entity_id"] == "entity-1"


def test_summarize_entity_cached_second_call_with_same_hash_reuses_first_result():
    """The re-ingestion scenario tasks.md Phase 5 asks to confirm: an unchanged entity
    (same content_hash) summarized again under a *different* job/entity id must still
    skip the LLM call, but must land its own queryable knowledge_summaries row for the
    new job — not just silently return the first job's row untouched."""
    llm_client = _FakeAzureClient(json.dumps({"summary": "Does a thing.", "risk_level": "low", "risk_factors": []}))
    client = FakeClient(org_id="org-123")

    first = summarize_entity_cached(
        client,
        job_id="job-1",
        org_id="org-123",
        entity=_entity(),
        entity_db_id="entity-1",
        content_hash="hash-1",
        llm_client=llm_client,
        embed=_fake_embed,
    )
    second = summarize_entity_cached(
        client,
        job_id="job-2",
        org_id="org-123",
        entity=_entity(),
        entity_db_id="entity-2",
        content_hash="hash-1",
        llm_client=llm_client,
        embed=_fake_embed,
    )

    assert len(llm_client.chat.completions.calls) == 1
    assert second["id"] != first["id"]
    assert second["summary_text"] == first["summary_text"]
    assert second["job_id"] == "job-2"
    assert second["entity_id"] == "entity-2"
    assert len(client.table("knowledge_summaries").rows) == 2

    # summary_cache still maps hash-1 -> the original (first-seen) summary.
    [cache_row] = client.table("summary_cache").rows
    assert cache_row["summary_id"] == first["id"]
