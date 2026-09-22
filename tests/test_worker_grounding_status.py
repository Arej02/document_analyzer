from document_analyzer.supabase_client import insert_scoped
from document_analyzer.worker.ground import GroundingResult
from document_analyzer.worker.ground_retry import GroundingRetryResult
from document_analyzer.worker.grounding_status import grounding_status_for, persist_grounding_status

from fakes import FakeClient


def _retry_result(verdict, retries_used=0, issues=None):
    return GroundingRetryResult(
        summary=object(),
        grounding=GroundingResult(verdict=verdict, issues=issues or []),
        retries_used=retries_used,
    )


def test_grounding_status_for_grounded_result_is_verified():
    assert grounding_status_for(_retry_result("grounded")) == "verified"


def test_grounding_status_for_exhausted_retries_is_unverified():
    assert grounding_status_for(_retry_result("ungrounded", retries_used=2, issues=["still off"])) == "unverified"


def test_persist_grounding_status_writes_verified_on_grounded_result():
    client = FakeClient(org_id="org-123")
    summary = insert_scoped(
        client, "knowledge_summaries", "org-123", {"job_id": "job-1", "level": "entity", "summary_text": "x", "risk_level": "low"}
    ).data[0]

    persist_grounding_status(client, summary["id"], "org-123", _retry_result("grounded"))

    [row] = client.table("knowledge_summaries").rows
    assert row["grounding_status"] == "verified"


def test_persist_grounding_status_writes_unverified_on_exhausted_retries():
    client = FakeClient(org_id="org-123")
    summary = insert_scoped(
        client, "knowledge_summaries", "org-123", {"job_id": "job-1", "level": "entity", "summary_text": "x", "risk_level": "high"}
    ).data[0]

    persist_grounding_status(client, summary["id"], "org-123", _retry_result("ungrounded", retries_used=2))

    [row] = client.table("knowledge_summaries").rows
    assert row["grounding_status"] == "unverified"


def test_persist_grounding_status_does_not_raise_on_ungrounded_outcome():
    client = FakeClient(org_id="org-123")
    summary = insert_scoped(
        client, "knowledge_summaries", "org-123", {"job_id": "job-1", "level": "entity", "summary_text": "x", "risk_level": "high"}
    ).data[0]

    persist_grounding_status(client, summary["id"], "org-123", _retry_result("ungrounded", retries_used=2))


def test_persist_grounding_status_does_not_touch_another_orgs_summary():
    client = FakeClient(org_id="org-123")
    summary = insert_scoped(
        client, "knowledge_summaries", "org-A", {"job_id": "job-1", "level": "entity", "summary_text": "x", "risk_level": "low"}
    ).data[0]

    persist_grounding_status(client, summary["id"], "org-B", _retry_result("grounded"))

    [row] = client.table("knowledge_summaries").rows
    assert row.get("grounding_status") is None
