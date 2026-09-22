from document_analyzer.worker.secret_scan import Finding, log_findings, scan_and_redact

from fakes import FakeClient


def test_scan_leaves_ordinary_values_untouched():
    raw_facts = {"params": ["a", "b"], "line": 5}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted == raw_facts
    assert findings == []


def test_scan_redacts_aws_access_key():
    raw_facts = {"note": "key is AKIAABCDEFGHIJKLMNOP for prod"}

    redacted, findings = scan_and_redact(raw_facts)

    # The whole field is replaced, not just the matched substring — safer than
    # partial substitution, which risks leaving a secret fragment behind if a
    # pattern's match boundary is ever slightly off.
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted["note"]
    assert redacted["note"] == "[REDACTED:aws_access_key]"
    assert findings == [Finding(field_path="note", reason="aws_access_key")]


def test_scan_redacts_github_token():
    raw_facts = {"args": {"token": "ghp_" + "a" * 36}}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted["args"]["token"] == "[REDACTED:github_token]"
    assert findings == [Finding(field_path="args.token", reason="github_token")]


def test_scan_redacts_private_key_block():
    raw_facts = {"cert": "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----"}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted["cert"].startswith("[REDACTED:private_key_block]")
    assert findings[0].reason == "private_key_block"


def test_scan_flags_hardcoded_credential_field_by_key_name():
    raw_facts = {"args": {"password": "hunter2hunter2"}}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted["args"]["password"] == "[REDACTED:hardcoded_credential_field]"
    assert findings == [Finding(field_path="args.password", reason="hardcoded_credential_field")]


def test_scan_does_not_flag_short_or_empty_credential_field_values():
    raw_facts = {"password": "", "pwd": "ab"}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted == raw_facts
    assert findings == []


def test_scan_walks_lists_and_nested_dicts_and_records_field_path():
    raw_facts = {"methods": [{"name": "connect", "args": {"secret": "supersecretvalue"}}]}

    redacted, findings = scan_and_redact(raw_facts)

    assert redacted["methods"][0]["args"]["secret"] == "[REDACTED:hardcoded_credential_field]"
    assert findings == [Finding(field_path="methods.0.args.secret", reason="hardcoded_credential_field")]


def test_scan_does_not_mutate_input():
    raw_facts = {"note": "AKIAABCDEFGHIJKLMNOP"}

    scan_and_redact(raw_facts)

    assert raw_facts["note"] == "AKIAABCDEFGHIJKLMNOP"


def test_log_findings_writes_rows_scoped_to_org_and_job():
    client = FakeClient(org_id="org-123")
    findings_by_entity_id = {
        "entity-1": [Finding(field_path="args.password", reason="hardcoded_credential_field")],
        "entity-2": [Finding(field_path="note", reason="aws_access_key")],
    }

    log_findings(client, job_id="job-1", org_id="org-123", findings_by_entity_id=findings_by_entity_id)

    rows = client.table("secret_findings").rows
    assert len(rows) == 2
    assert all(r["org_id"] == "org-123" and r["job_id"] == "job-1" for r in rows)
    by_entity = {r["entity_id"]: r for r in rows}
    assert by_entity["entity-1"]["reason"] == "hardcoded_credential_field"
    assert by_entity["entity-2"]["field_path"] == "note"


def test_log_findings_no_findings_is_a_no_op():
    client = FakeClient(org_id="org-123")

    log_findings(client, job_id="job-1", org_id="org-123", findings_by_entity_id={})

    assert client.table("secret_findings").rows == []
