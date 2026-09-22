from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult
from document_analyzer.worker.persist import content_hash_for, persist_extraction_result

from fakes import FakeClient


def test_persist_writes_entities_scoped_to_org_and_job():
    client = FakeClient(org_id="org-123")
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                local_id="function:app.php:do_thing:3",
                entity_type="function",
                name="do_thing",
                file_path="app.php",
                raw_facts={"params": ["a"]},
            )
        ]
    )

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    [row] = client.table("extracted_entities").rows
    assert row["org_id"] == "org-123"
    assert row["job_id"] == "job-1"
    assert row["entity_type"] == "function"
    assert row["name"] == "do_thing"
    assert row["file_path"] == "app.php"
    assert row["raw_facts"] == {"params": ["a"]}
    assert row["content_hash"] == content_hash_for("function", "do_thing", "app.php", {"params": ["a"]})


def test_persist_resolves_edge_to_entity_ids():
    client = FakeClient(org_id="org-123")
    caller = ExtractedEntity(
        local_id="function:app.php:do_thing:3", entity_type="function", name="do_thing", file_path="app.php"
    )
    callee = ExtractedEntity(
        local_id="function:app.php:helper:8", entity_type="function", name="helper", file_path="app.php"
    )
    result = ExtractionResult(
        entities=[caller, callee],
        edges=[ExtractedEdge(from_local_id=caller.local_id, edge_type="calls", to_local_id=callee.local_id)],
    )

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    entity_rows = {row["name"]: row for row in client.table("extracted_entities").rows}
    [edge_row] = client.table("entity_edges").rows
    assert edge_row["org_id"] == "org-123"
    assert edge_row["job_id"] == "job-1"
    assert edge_row["edge_type"] == "calls"
    assert edge_row["from_entity_id"] == entity_rows["do_thing"]["id"]
    assert edge_row["to_entity_id"] == entity_rows["helper"]["id"]


def test_persist_edge_with_no_target_keeps_to_entity_id_null():
    client = FakeClient(org_id="org-123")
    caller = ExtractedEntity(
        local_id="function:app.php:bootstrap:1", entity_type="function", name="bootstrap", file_path="app.php"
    )
    result = ExtractionResult(
        entities=[caller],
        edges=[ExtractedEdge(from_local_id=caller.local_id, edge_type="include", to_local_id=None)],
    )

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    [edge_row] = client.table("entity_edges").rows
    assert edge_row["edge_type"] == "include"
    assert edge_row["to_entity_id"] is None


def test_persist_no_entities_is_a_no_op():
    client = FakeClient(org_id="org-123")

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=ExtractionResult())

    assert client.table("extracted_entities").rows == []
    assert client.table("entity_edges").rows == []


def test_persist_entities_without_edges_does_not_touch_entity_edges_table():
    client = FakeClient(org_id="org-123")
    result = ExtractionResult(
        entities=[
            ExtractedEntity(local_id="function:app.php:solo:1", entity_type="function", name="solo", file_path="app.php")
        ]
    )

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    assert len(client.table("extracted_entities").rows) == 1
    assert client.table("entity_edges").rows == []


def test_content_hash_is_stable_and_sensitive_to_raw_facts():
    a = content_hash_for("function", "do_thing", "app.php", {"params": ["a", "b"]})
    b = content_hash_for("function", "do_thing", "app.php", {"params": ["a", "b"]})
    c = content_hash_for("function", "do_thing", "app.php", {"params": ["a"]})

    assert a == b
    assert a != c


def test_persist_redacts_secret_in_raw_facts_before_writing_entity():
    client = FakeClient(org_id="org-123")
    entity = ExtractedEntity(
        local_id="schema:app.php:cpt:book:1",
        entity_type="schema",
        name="book",
        file_path="app.php",
        raw_facts={"args": {"api_key": "AKIAABCDEFGHIJKLMNOP"}},
    )
    result = ExtractionResult(entities=[entity])

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    [row] = client.table("extracted_entities").rows
    assert row["raw_facts"]["args"]["api_key"] == "[REDACTED:aws_access_key]"


def test_persist_logs_finding_against_the_entitys_real_id():
    client = FakeClient(org_id="org-123")
    entity = ExtractedEntity(
        local_id="function:app.php:connect:1",
        entity_type="function",
        name="connect",
        file_path="app.php",
        raw_facts={"password": "hunter2hunter2"},
    )
    result = ExtractionResult(entities=[entity])

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    [entity_row] = client.table("extracted_entities").rows
    [finding_row] = client.table("secret_findings").rows
    assert finding_row["org_id"] == "org-123"
    assert finding_row["job_id"] == "job-1"
    assert finding_row["entity_id"] == entity_row["id"]
    assert finding_row["field_path"] == "password"
    assert finding_row["reason"] == "hardcoded_credential_field"


def test_persist_no_secrets_leaves_secret_findings_table_untouched():
    client = FakeClient(org_id="org-123")
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                local_id="function:app.php:solo:1",
                entity_type="function",
                name="solo",
                file_path="app.php",
                raw_facts={"params": ["a"]},
            )
        ]
    )

    persist_extraction_result(client, job_id="job-1", org_id="org-123", result=result)

    assert client.table("secret_findings").rows == []
