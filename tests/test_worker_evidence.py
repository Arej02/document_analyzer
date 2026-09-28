"""Source evidence (worker/evidence.py), its wiring into persistence (persist.py) and the
worker's extraction step (run.py `_run_extractors`)."""

from pathlib import Path

from document_analyzer.worker import embed as embed_module
from document_analyzer.worker import run as run_module
from document_analyzer.worker.evidence import build_evidence, persist_evidence
from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult
from document_analyzer.worker.persist import content_hash_for, persist_extraction_result
from document_analyzer.worker.secret_scan import redact_text
from document_analyzer.worker.summarize import _entity_facts_payload

from fakes import FakeClient

PHP = """<?php
function connect() {
    $password = "hunter22-secret";
    return new PDO("mysql:host=db", "root", $password);
}

function unrelated() {
    return 1;
}
"""

PY = """import os


@decorator
def helper(x):
    return x + 1


class Greeter:
    def greet(self):
        return "hi"
"""


def _fake_embed_many(texts):
    return [[float(len(t)), 0.0] for t in texts]


def _entity(local_id, name, file_path, **facts):
    return ExtractedEntity(local_id=local_id, entity_type="function", name=name, file_path=file_path, raw_facts=facts)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "db.php").write_text(PHP)
    (tmp_path / "other.php").write_text("<?php\necho 'no entities here';\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "util.py").write_text(PY)
    (tmp_path / "notes.md").write_text("# not a language extraction touched\n")
    return tmp_path


def _entities():
    return [
        ((0, "connect"), _entity("connect", "connect", "db.php", line=2, end_line=5)),
        ((0, "unrelated"), _entity("unrelated", "unrelated", "db.php", line=7, end_line=9)),
        ((1, "helper"), _entity("helper", "helper", "pkg/util.py", line=4)),  # start line only
        ((1, "nowhere"), _entity("nowhere", "nowhere", "pkg/util.py")),  # no location at all
    ]


def test_redact_text_replaces_only_the_secret():
    text, reasons = redact_text('$password = "hunter22-secret";\n$key = "AKIAABCDEFGHIJKLMNOP";\n$user = "root";')
    assert text == (
        '$password = "[REDACTED:hardcoded_credential_field]";\n$key = "[REDACTED:aws_access_key]";\n$user = "root";'
    )
    assert sorted(reasons) == ["aws_access_key", "hardcoded_credential_field"]


def test_build_evidence_uses_verified_ranges_and_windows(tmp_path):
    build = build_evidence(_repo(tmp_path), _entities())
    by_range = {(c.file_path, c.line_start, c.line_end): c for c in build.chunks}

    connect = by_range[("db.php", 2, 5)]
    assert connect.strategy == "entity_range"
    assert connect.entities == [((0, "connect"), "entity_range")]
    # Python: the stdlib parser verifies helper's full range from its decorator line.
    helper = by_range[("pkg/util.py", 4, 6)]
    assert helper.strategy == "entity_range" and helper.entities == [((1, "helper"), "entity_range")]
    # Lines outside any entity range are still covered, as windows.
    assert any(c.file_path == "pkg/util.py" and c.strategy == "source_window" for c in build.chunks)
    # A same-language file without entities is chunked too; other languages aren't.
    assert any(c.file_path == "other.php" for c in build.chunks)
    assert not any(c.file_path == "notes.md" for c in build.chunks)
    assert build.report["entity_range_basis"] == {"extractor_range": 2, "python_ast": 1, "no_location": 1}


def test_build_evidence_redacts_secrets_in_chunks_and_excerpts(tmp_path):
    build = build_evidence(_repo(tmp_path), _entities())
    connect = next(c for c in build.chunks if c.line_start == 2 and c.file_path == "db.php")

    assert "hunter22" not in connect.content
    assert connect.redactions == ["hardcoded_credential_field"]
    assert "hunter22" not in build.excerpts[(0, "connect")]["text"]
    assert build.report["redacted_chunks"] == 1


def test_excerpts_cover_the_definition_or_mark_themselves_partial(tmp_path):
    build = build_evidence(_repo(tmp_path), _entities())

    assert build.excerpts[(0, "connect")]["complete_definition"] is True
    assert build.excerpts[(0, "connect")]["line_start"] == 2 and build.excerpts[(0, "connect")]["line_end"] == 5
    assert (1, "nowhere") not in build.excerpts


def test_excerpt_hash_changes_the_content_hash():
    base = content_hash_for("function", "f", "a.php", {"calls": []})
    assert content_hash_for("function", "f", "a.php", {"calls": []}, None) == base
    assert content_hash_for("function", "f", "a.php", {"calls": []}, "abc") != base


def test_facts_payload_carries_the_excerpt_only_when_there_is_one():
    entity = _entity("f", "f", "a.php", line=1)
    assert "source_excerpt" not in _entity_facts_payload(entity)
    payload = _entity_facts_payload(
        entity, {"line_start": 1, "line_end": 3, "complete_definition": True, "text": "code", "sha256": "x"}
    )
    assert payload["source_excerpt"] == {"line_start": 1, "line_end": 3, "complete_definition": True, "text": "code"}


def test_persist_extraction_result_returns_ids_and_keeps_edge_provenance():
    client = FakeClient(org_id="org-1")
    result = ExtractionResult(
        entities=[_entity("a", "a", "a.py", line=1), _entity("b", "b", "a.py", line=5)],
        edges=[ExtractedEdge("a", "calls", "b", provenance={"extractor": "graphify", "confidence": "INFERRED"})],
    )
    ids = persist_extraction_result(client, "job-1", "org-1", result, excerpt_hashes={"a": "h"})

    rows = {r["name"]: r for r in client.table("extracted_entities").rows}
    assert ids == {"a": rows["a"]["id"], "b": rows["b"]["id"]}
    assert rows["a"]["content_hash"] == content_hash_for("function", "a", "a.py", {"line": 1}, "h")
    [edge] = client.table("entity_edges").rows
    assert edge["provenance"] == {"extractor": "graphify", "confidence": "INFERRED"}


def test_persist_extraction_result_omits_provenance_for_php_family_edges():
    client = FakeClient(org_id="org-1")
    result = ExtractionResult(entities=[_entity("a", "a", "a.php")], edges=[ExtractedEdge("a", "include")])
    persist_extraction_result(client, "job-1", "org-1", result)
    [edge] = client.table("entity_edges").rows
    assert "provenance" not in edge


def test_persist_evidence_writes_chunks_links_and_findings(tmp_path):
    client = FakeClient(org_id="org-1")
    build = build_evidence(_repo(tmp_path), _entities())
    entity_ids = {(0, "connect"): "ent-connect", (1, "helper"): "ent-helper"}

    written = persist_evidence(
        client, "job-1", "org-1", build, entity_ids, {(0, "connect"): "connect"}, embed_many=_fake_embed_many
    )

    chunks = client.table("source_chunks").rows
    assert written == len(chunks) == len(build.chunks)
    assert all(r["org_id"] == "org-1" and r["job_id"] == "job-1" and r["embedding"] for r in chunks)
    links = client.table("source_chunk_entities").rows
    # Only entities that were actually persisted are linked ("unrelated" isn't in entity_ids).
    assert {r["entity_id"] for r in links} == {"ent-connect", "ent-helper"}
    [finding] = client.table("secret_findings").rows
    assert finding["entity_id"] == "ent-connect"
    assert finding["field_path"] == "source:db.php:L2-L5"
    assert "hunter22" not in str(finding)


class _StubExtractor:
    def __init__(self, result):
        self._result = result

    def detect(self, _repo_path):
        return True

    def extract(self, _repo_path):
        return self._result


def test_run_extractors_persists_entities_evidence_and_coverage(tmp_path, monkeypatch):
    client = FakeClient(org_id="org-1")
    client.table("ingestion_jobs").insert({"id": "job-1", "org_id": "org-1", "status": "running"}).execute()
    php = ExtractionResult(entities=[_entity("connect", "connect", "db.php", line=2, end_line=5)])
    graphify = ExtractionResult(
        entities=[_entity("e1", "helper", "pkg/util.py", line=4, extractor="graphify")],
        coverage={"graphify": {"status": "ran", "files_requested": 1}},
    )
    monkeypatch.setattr(run_module, "_extractors", lambda job_id: (_StubExtractor(php), _StubExtractor(graphify)))
    monkeypatch.setattr(embed_module, "embed_texts", _fake_embed_many)

    excerpts = run_module._run_extractors(_repo(tmp_path), client, "job-1", "org-1")

    entities = {r["name"]: r for r in client.table("extracted_entities").rows}
    assert set(excerpts) == {entities["connect"]["id"], entities["helper"]["id"]}
    assert "hunter22" not in excerpts[entities["connect"]["id"]]["text"]
    assert client.table("source_chunks").rows
    [job] = client.table("ingestion_jobs").rows
    assert job["extraction_coverage"]["graphify"] == {"status": "ran", "files_requested": 1}
    assert job["extraction_coverage"]["evidence"]["chunks"] == len(client.table("source_chunks").rows)
