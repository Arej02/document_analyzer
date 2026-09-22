from document_analyzer.entities import get_entity_detail, list_job_entities
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient


def _entity(client, org_id, job_id, entity_type, name, file_path="a.php", raw_facts=None):
    return insert_scoped(
        client,
        "extracted_entities",
        org_id,
        {"job_id": job_id, "entity_type": entity_type, "name": name, "file_path": file_path, "raw_facts": raw_facts if raw_facts is not None else {}},
    ).data[0]


def _edge(client, org_id, job_id, from_entity_id, to_entity_id, edge_type):
    return insert_scoped(
        client,
        "entity_edges",
        org_id,
        {"job_id": job_id, "from_entity_id": from_entity_id, "to_entity_id": to_entity_id, "edge_type": edge_type},
    ).data[0]


def _summary(client, org_id, job_id, entity_id, summary_text, risk_level, grounding_status=None):
    row = {"job_id": job_id, "level": "entity", "entity_id": entity_id, "summary_text": summary_text, "risk_level": risk_level}
    if grounding_status is not None:
        row["grounding_status"] = grounding_status
    return insert_scoped(client, "knowledge_summaries", org_id, row).data[0]


def test_returns_entities_joined_with_their_entity_level_summary():
    client = FakeClient(org_id="org-123")
    e1 = _entity(client, "org-123", "job-1", "function", "process_payment")
    _summary(client, "org-123", "job-1", e1["id"], "Charges the customer.", "high", "verified")

    result = list_job_entities(client, "org-123", "job-1")

    assert result["entities"] == [
        {
            "id": e1["id"],
            "entity_type": "function",
            "name": "process_payment",
            "file_path": "a.php",
            "summary_text": "Charges the customer.",
            "risk_level": "high",
            "grounding_status": "verified",
        }
    ]
    assert result["total_count"] == 1
    assert result["limit"] == 50
    assert result["offset"] == 0


def test_entity_without_a_summary_yet_has_null_summary_fields():
    client = FakeClient(org_id="org-123")
    e1 = _entity(client, "org-123", "job-1", "function", "process_payment")

    result = list_job_entities(client, "org-123", "job-1")

    assert result["entities"] == [
        {
            "id": e1["id"],
            "entity_type": "function",
            "name": "process_payment",
            "file_path": "a.php",
            "summary_text": None,
            "risk_level": None,
            "grounding_status": None,
        }
    ]


def test_filters_by_entity_type():
    client = FakeClient(org_id="org-123")
    _entity(client, "org-123", "job-1", "function", "f")
    _entity(client, "org-123", "job-1", "class", "C")

    result = list_job_entities(client, "org-123", "job-1", entity_type="class")

    assert [e["name"] for e in result["entities"]] == ["C"]
    assert result["total_count"] == 1


def test_filters_by_risk_level_from_the_joined_summary():
    client = FakeClient(org_id="org-123")
    e1 = _entity(client, "org-123", "job-1", "function", "low_risk_fn")
    e2 = _entity(client, "org-123", "job-1", "function", "high_risk_fn")
    _summary(client, "org-123", "job-1", e1["id"], "A getter.", "low")
    _summary(client, "org-123", "job-1", e2["id"], "Runs a shell command.", "high")

    result = list_job_entities(client, "org-123", "job-1", risk_level="high")

    assert [e["name"] for e in result["entities"]] == ["high_risk_fn"]
    assert result["total_count"] == 1


def test_risk_level_filter_excludes_entities_with_no_summary_yet():
    client = FakeClient(org_id="org-123")
    _entity(client, "org-123", "job-1", "function", "unsummarized_fn")

    result = list_job_entities(client, "org-123", "job-1", risk_level="high")

    assert result["entities"] == []
    assert result["total_count"] == 0


def test_pagination_slices_the_filtered_result_and_reports_total_count():
    client = FakeClient(org_id="org-123")
    for i in range(5):
        _entity(client, "org-123", "job-1", "function", f"fn_{i}")

    result = list_job_entities(client, "org-123", "job-1", limit=2, offset=2)

    assert [e["name"] for e in result["entities"]] == ["fn_2", "fn_3"]
    assert result["total_count"] == 5
    assert result["limit"] == 2
    assert result["offset"] == 2


def test_only_returns_this_orgs_and_this_jobs_entities():
    client = FakeClient(org_id="org-123")
    _entity(client, "org-123", "job-1", "function", "mine")
    _entity(client, "org-other", "job-1", "function", "other_org")
    _entity(client, "org-123", "job-2", "function", "other_job")

    result = list_job_entities(client, "org-123", "job-1")

    assert [e["name"] for e in result["entities"]] == ["mine"]


def test_get_entity_detail_returns_raw_facts_and_summary():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-123", "job-1", "function", "process_payment", raw_facts={"calls": ["curl_exec"]})
    _summary(client, "org-123", "job-1", entity["id"], "Charges the customer.", "high", "verified")

    result = get_entity_detail(client, "org-123", "job-1", entity["id"])

    assert result["id"] == entity["id"]
    assert result["entity_type"] == "function"
    assert result["name"] == "process_payment"
    assert result["file_path"] == "a.php"
    assert result["raw_facts"] == {"calls": ["curl_exec"]}
    assert result["summary_text"] == "Charges the customer."
    assert result["risk_level"] == "high"
    assert result["grounding_status"] == "verified"
    assert result["edges"] == {"outgoing": [], "incoming": []}


def test_get_entity_detail_null_summary_fields_when_not_summarized_yet():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-123", "job-1", "function", "unsummarized")

    result = get_entity_detail(client, "org-123", "job-1", entity["id"])

    assert result["summary_text"] is None
    assert result["risk_level"] is None
    assert result["grounding_status"] is None


def test_get_entity_detail_returns_none_for_missing_entity():
    client = FakeClient(org_id="org-123")

    assert get_entity_detail(client, "org-123", "job-1", "does-not-exist") is None


def test_get_entity_detail_returns_none_for_another_orgs_entity():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-other", "job-1", "function", "not_mine")

    assert get_entity_detail(client, "org-123", "job-1", entity["id"]) is None


def test_get_entity_detail_returns_none_for_another_jobs_entity():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-123", "job-2", "function", "not_this_job")

    assert get_entity_detail(client, "org-123", "job-1", entity["id"]) is None


def test_get_entity_detail_includes_outgoing_and_incoming_edges():
    client = FakeClient(org_id="org-123")
    caller = _entity(client, "org-123", "job-1", "function", "checkout")
    callee = _entity(client, "org-123", "job-1", "function", "charge_card")
    other_caller = _entity(client, "org-123", "job-1", "function", "cart_add")
    _edge(client, "org-123", "job-1", caller["id"], callee["id"], "calls")
    _edge(client, "org-123", "job-1", other_caller["id"], caller["id"], "calls")

    result = get_entity_detail(client, "org-123", "job-1", caller["id"])

    assert len(result["edges"]["outgoing"]) == 1
    outgoing = result["edges"]["outgoing"][0]
    assert outgoing["edge_type"] == "calls"
    assert outgoing["to_entity"]["id"] == callee["id"]
    assert outgoing["to_entity"]["name"] == "charge_card"

    assert len(result["edges"]["incoming"]) == 1
    incoming = result["edges"]["incoming"][0]
    assert incoming["edge_type"] == "calls"
    assert incoming["from_entity"]["id"] == other_caller["id"]
    assert incoming["from_entity"]["name"] == "cart_add"


def test_get_entity_detail_outgoing_edge_with_no_target_has_null_to_entity():
    client = FakeClient(org_id="org-123")
    entity = _entity(client, "org-123", "job-1", "function", "loader")
    _edge(client, "org-123", "job-1", entity["id"], None, "include")

    result = get_entity_detail(client, "org-123", "job-1", entity["id"])

    assert result["edges"]["outgoing"] == [{"id": result["edges"]["outgoing"][0]["id"], "edge_type": "include", "to_entity": None}]
