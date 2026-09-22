"""Worker step: persist extraction output — tasks.md Phase 2
"Persist extracted_entities and entity_edges from extraction output".

Runs on the async worker only, via the service-role client (bypasses RLS) —
see supabase_client.get_service_role_client. Takes one Extractor's
ExtractionResult (extract.py) at a time and writes it into `extracted_entities`
/ `entity_edges` (design.md §2), stamping org_id from the job, never from
ingested content, per CLAUDE.md's cross-tenant-leak warning for worker writes.

Call once per extractor that ran against a job (PhpExtractor, WordPressExtractor,
...) — an `ExtractionResult`'s `local_id`s are only unique *within* that one
result (extract.py), so entities/edges from different extractors are persisted
in separate batches rather than merged into one local_id space here.

Every entity's `raw_facts` is run through the secret/credential scan
(secret_scan.py, specs.md NFR2) before it's ever written — a match is redacted
in place and logged to `secret_findings` once the entity's real id is known.

`content_hash` is computed from the entity's own content (type/name/file_path/
*redacted* raw_facts, since that's what's actually persisted) — it's what
`summary_cache` (Phase 3) will key on to skip re-summarizing unchanged
entities across re-ingestions.
"""

import hashlib
import json

from supabase import Client

from document_analyzer.supabase_client import insert_many_scoped
from document_analyzer.worker.extract import ExtractionResult
from document_analyzer.worker.secret_scan import Finding, log_findings, scan_and_redact


def content_hash_for(entity_type: str, name: str, file_path: str, raw_facts: dict) -> str:
    canonical = json.dumps(
        {"entity_type": entity_type, "name": name, "file_path": file_path, "raw_facts": raw_facts},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def persist_extraction_result(client: Client, job_id: str, org_id: str, result: ExtractionResult) -> None:
    """Inserts one extractor's entities, then its edges resolved against the new entity ids."""
    if not result.entities:
        return

    entity_rows = []
    findings_by_local_id: dict[str, list[Finding]] = {}
    for entity in result.entities:
        redacted_raw_facts, findings = scan_and_redact(entity.raw_facts)
        if findings:
            findings_by_local_id[entity.local_id] = findings
        entity_rows.append(
            {
                "job_id": job_id,
                "entity_type": entity.entity_type,
                "name": entity.name,
                "file_path": entity.file_path,
                "raw_facts": redacted_raw_facts,
                "content_hash": content_hash_for(entity.entity_type, entity.name, entity.file_path, redacted_raw_facts),
            }
        )

    # A single multi-row INSERT preserves input order, so this zip recovers
    # each entity's new db id without a second round-trip per row.
    inserted = insert_many_scoped(client, "extracted_entities", org_id, entity_rows).data
    local_id_to_db_id = {entity.local_id: row["id"] for entity, row in zip(result.entities, inserted)}

    if findings_by_local_id:
        findings_by_entity_id = {
            local_id_to_db_id[local_id]: findings for local_id, findings in findings_by_local_id.items()
        }
        log_findings(client, job_id, org_id, findings_by_entity_id)

    if not result.edges:
        return

    edge_rows = []
    for edge in result.edges:
        from_entity_id = local_id_to_db_id.get(edge.from_local_id)
        if from_entity_id is None:
            # Shouldn't happen: an edge's from_local_id always names an entity
            # this same ExtractionResult produced. Skip rather than persist a
            # dangling reference if that invariant is ever violated.
            continue
        edge_rows.append(
            {
                "job_id": job_id,
                "from_entity_id": from_entity_id,
                "to_entity_id": local_id_to_db_id.get(edge.to_local_id) if edge.to_local_id else None,
                "edge_type": edge.edge_type,
            }
        )

    if edge_rows:
        insert_many_scoped(client, "entity_edges", org_id, edge_rows)
