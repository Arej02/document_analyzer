"""GET /api/agent1/jobs/{job_id}/entities — design.md §4.

Paginated list of `extracted_entities` for one job, each annotated with its
entity-level `knowledge_summaries` fields (`summary_text`, `risk_level`,
`grounding_status`) — the knowledge base browser view design.md §6 calls for.
Supports `?entity_type=` (a direct `extracted_entities.entity_type` filter)
and `?risk_level=` — `risk_level` lives on the entity's summary, not on
`extracted_entities` itself, so that filter is applied in Python after
joining rather than as a second `.eq()` at the query level.

Pagination (`?limit=`/`?offset=`) is applied in Python, over the already
filtered+joined result set, rather than a DB-level LIMIT/OFFSET/join —
simplest option that's still correct, matching how `list_projects`
(projects.py) already does an unpaginated full scan for this MVP's scale.
Revisit (DB-level pagination, an actual SQL join) if Phase 6 load testing
(5,000-10,000+ entities) shows this doesn't hold up.

`summary_text`/`risk_level`/`grounding_status` come back `None` for an entity
whose entity-level summary hasn't been persisted yet (e.g. querying mid-
ingestion, before summarize_entity_cached + persist_summary have run for it)
— this endpoint doesn't gate on job status the way `/summary` does.

Per-entity `raw_facts` and edges are `get_entity_detail`, below, backing
`GET /jobs/{job_id}/entities/{entity_id}` — design.md §4 named that endpoint
("raw facts, summary, grounding status, and its edges (both directions)")
without pinning down a shape either, so that shape is decided here too, same
as the list endpoint above, and mirrored in design.md §4.
"""

from supabase import Client

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_ENTITY_COLUMNS = "id, entity_type, name, file_path"
_ENTITY_DETAIL_COLUMNS = "id, entity_type, name, file_path, raw_facts"
_SUMMARY_COLUMNS = "entity_id, summary_text, risk_level, grounding_status"
_ENTITY_DETAIL_SUMMARY_COLUMNS = "summary_text, risk_level, grounding_status"
_EDGE_COLUMNS = "id, edge_type, from_entity_id, to_entity_id"


def list_job_entities(
    client: Client,
    org_id: str,
    job_id: str,
    entity_type: str | None = None,
    risk_level: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """Filtered, paginated entities + their entity-level summary fields for one job."""
    entity_query = client.table("extracted_entities").select(_ENTITY_COLUMNS).eq("org_id", org_id).eq("job_id", job_id)
    if entity_type is not None:
        entity_query = entity_query.eq("entity_type", entity_type)
    entities = entity_query.execute().data

    summaries = (
        client.table("knowledge_summaries")
        .select(_SUMMARY_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("level", "entity")
        .execute()
    ).data
    summary_by_entity_id = {row["entity_id"]: row for row in summaries}

    joined = []
    for entity in entities:
        summary = summary_by_entity_id.get(entity["id"])
        joined.append(
            {
                "id": entity["id"],
                "entity_type": entity["entity_type"],
                "name": entity["name"],
                "file_path": entity["file_path"],
                "summary_text": summary.get("summary_text") if summary else None,
                "risk_level": summary.get("risk_level") if summary else None,
                "grounding_status": summary.get("grounding_status") if summary else None,
            }
        )

    if risk_level is not None:
        joined = [row for row in joined if row["risk_level"] == risk_level]

    total_count = len(joined)
    page = joined[offset : offset + limit]

    return {"entities": page, "total_count": total_count, "limit": limit, "offset": offset}


def _entity_stub(client: Client, org_id: str, entity_id: str) -> dict | None:
    """The small {id, entity_type, name, file_path} shape used to identify the *other*
    entity on each side of an edge — never that entity's own raw_facts/summary, which
    would make an edge list balloon into a second full entity list. Explicitly rebuilt
    from the row rather than returned as-is, so this stays the small shape even though
    `.select()` already asks the DB to project only these columns."""
    rows = (
        client.table("extracted_entities")
        .select(_ENTITY_COLUMNS)
        .eq("org_id", org_id)
        .eq("id", entity_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return None
    row = rows[0]
    return {"id": row["id"], "entity_type": row["entity_type"], "name": row["name"], "file_path": row["file_path"]}


def get_entity_detail(client: Client, org_id: str, job_id: str, entity_id: str) -> dict | None:
    """One entity's full detail: raw_facts, its entity-level summary fields, and every
    edge touching it in either direction — `None` if the entity doesn't exist / isn't
    this org's or this job's.

    `edges.outgoing` are edges where this entity is `from_entity_id` (paired with the
    edge's target, `to_entity` — `None` for a `to_entity_id`-less edge to an external/
    global target, design.md §2's documented nullable case); `edges.incoming` are edges
    where this entity is `to_entity_id` (paired with `from_entity`, which always exists
    — `from_entity_id` is NOT NULL). Each side's other entity is the small stub shape
    from `_entity_stub`, not a full entity — following an edge is "what does this touch
    / what touches this", not a second copy of the list endpoint.
    """
    entity_rows = (
        client.table("extracted_entities")
        .select(_ENTITY_DETAIL_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("id", entity_id)
        .limit(1)
        .execute()
    ).data
    if not entity_rows:
        return None
    entity = entity_rows[0]

    summary_rows = (
        client.table("knowledge_summaries")
        .select(_ENTITY_DETAIL_SUMMARY_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("level", "entity")
        .eq("entity_id", entity_id)
        .limit(1)
        .execute()
    ).data
    summary = summary_rows[0] if summary_rows else None

    outgoing = (
        client.table("entity_edges")
        .select(_EDGE_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("from_entity_id", entity_id)
        .execute()
    ).data
    incoming = (
        client.table("entity_edges")
        .select(_EDGE_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .eq("to_entity_id", entity_id)
        .execute()
    ).data

    return {
        "id": entity["id"],
        "entity_type": entity["entity_type"],
        "name": entity["name"],
        "file_path": entity["file_path"],
        "raw_facts": entity["raw_facts"],
        "summary_text": summary.get("summary_text") if summary else None,
        "risk_level": summary.get("risk_level") if summary else None,
        "grounding_status": summary.get("grounding_status") if summary else None,
        "edges": {
            "outgoing": [
                {
                    "id": edge["id"],
                    "edge_type": edge["edge_type"],
                    "to_entity": _entity_stub(client, org_id, edge["to_entity_id"]) if edge.get("to_entity_id") else None,
                }
                for edge in outgoing
            ],
            "incoming": [
                {
                    "id": edge["id"],
                    "edge_type": edge["edge_type"],
                    "from_entity": _entity_stub(client, org_id, edge["from_entity_id"]),
                }
                for edge in incoming
            ],
        },
    }
