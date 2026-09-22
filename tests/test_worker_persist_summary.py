import pytest

from document_analyzer.worker.persist_summary import persist_summary
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleSummary
from document_analyzer.worker.summarize_subsystem import RepoSummary, SubsystemSummary

from fakes import FakeClient


def _fake_embed(text):
    return [float(len(text)), 0.0, 1.0]


def test_persist_entity_summary_writes_scoped_row_with_entity_id():
    client = FakeClient(org_id="org-123")
    summary = EntitySummary(summary_text="Charges the customer.", risk_level="medium", risk_factors=["x"])

    row = persist_summary(
        client, job_id="job-1", org_id="org-123", summary=summary, entity_id="entity-1", embed=_fake_embed
    )

    [db_row] = client.table("knowledge_summaries").rows
    assert db_row is row
    assert db_row["org_id"] == "org-123"
    assert db_row["job_id"] == "job-1"
    assert db_row["level"] == "entity"
    assert db_row["entity_id"] == "entity-1"
    assert db_row["summary_text"] == "Charges the customer."
    assert db_row["risk_level"] == "medium"
    assert db_row["embedding"] == _fake_embed("Charges the customer.")


def test_persist_entity_summary_without_entity_id_raises():
    client = FakeClient(org_id="org-123")
    summary = EntitySummary(summary_text="Does a thing.", risk_level="low", risk_factors=[])

    with pytest.raises(ValueError):
        persist_summary(client, job_id="job-1", org_id="org-123", summary=summary, embed=_fake_embed)


def test_persist_module_summary_leaves_entity_id_null():
    client = FakeClient(org_id="org-123")
    summary = ModuleSummary(summary_text="Handles checkout.", risk_level="high", risk_factors=["process_payment"])

    persist_summary(client, job_id="job-1", org_id="org-123", summary=summary, embed=_fake_embed)

    [db_row] = client.table("knowledge_summaries").rows
    assert db_row["level"] == "module"
    assert db_row["entity_id"] is None


def test_persist_subsystem_summary_with_entity_id_raises():
    client = FakeClient(org_id="org-123")
    summary = SubsystemSummary(summary_text="Handles checkout.", risk_level="high", risk_factors=["checkout.php"])

    with pytest.raises(ValueError):
        persist_summary(
            client, job_id="job-1", org_id="org-123", summary=summary, entity_id="entity-1", embed=_fake_embed
        )


def test_persist_repo_summary_writes_repo_level_row():
    client = FakeClient(org_id="org-123")
    summary = RepoSummary(summary_text="A WordPress plugin.", risk_level="medium", risk_factors=["includes"])

    persist_summary(client, job_id="job-1", org_id="org-123", summary=summary, embed=_fake_embed)

    [db_row] = client.table("knowledge_summaries").rows
    assert db_row["level"] == "repo"
    assert db_row["entity_id"] is None
    assert db_row["risk_level"] == "medium"


def test_persist_summary_does_not_set_grounding_status():
    client = FakeClient(org_id="org-123")
    summary = ModuleSummary(summary_text="Handles checkout.", risk_level="low", risk_factors=[])

    persist_summary(client, job_id="job-1", org_id="org-123", summary=summary, embed=_fake_embed)

    [db_row] = client.table("knowledge_summaries").rows
    assert "grounding_status" not in db_row


def test_persist_summary_defaults_to_embed_text_when_none_given(monkeypatch):
    import document_analyzer.worker.persist_summary as persist_summary_module

    monkeypatch.setattr(persist_summary_module, "embed_text", lambda text: [9.0])
    client = FakeClient(org_id="org-123")
    summary = ModuleSummary(summary_text="Handles checkout.", risk_level="low", risk_factors=[])

    persist_summary(client, job_id="job-1", org_id="org-123", summary=summary)

    [db_row] = client.table("knowledge_summaries").rows
    assert db_row["embedding"] == [9.0]
