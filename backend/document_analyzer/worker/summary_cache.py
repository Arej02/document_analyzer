"""Worker step: summary_cache lookup/write-through — tasks.md Phase 3
"summary_cache lookup by content_hash before re-summarizing; write-through on
new summaries".

Closes out Phase 3. specs.md FR6: "`summary_cache` keyed by content hash
skips re-summarizing unchanged code on repeat ingestions." `content_hash`
here is `extracted_entities.content_hash` (persist.py::content_hash_for,
computed over an entity's own type/name/file_path/*redacted* raw_facts at
persist time) — the only content_hash concept the data model actually
defines (design.md §2). Module/subsystem/repo levels have no independent
content of their own to hash, and their own cache-invalidation story
("cascading re-summarization" when an ancestor's children change) is a
listed Phase 4 task, not this one.

`lookup_cached_summary`/`write_through_cache` are the two raw operations
against `summary_cache` (design.md §2: `content_hash` + `org_id` primary key
-> `summary_id`). `summarize_entity_cached` is the orchestration that ties
them to the rest of the entity pipeline: a cache hit still writes no new LLM
call (summarize.py::summarize_entity) and no new embedding (embed.py::embed_text)
— but it does write one new, cheap `knowledge_summaries` row
(`copy_cached_summary_forward`), scoped to *this* job and *this* job's own
`extracted_entities` row, copying the cached row's `summary_text`/`risk_level`/
`embedding`/`grounding_status` byte-for-byte. A miss runs the normal
summarize -> embed -> persist path and writes the new summary's id through to
`summary_cache` for the next ingestion to find.

**tasks.md Phase 5 "Confirm summary_cache correctly reuses unchanged entity
summaries" — gap found and fixed here:** the original Phase 3 version of this
module (and `worker/run.py::_process_one_entity`'s own, separately-written
cache-hit branch) returned the *existing* cached row as-is on a hit, with
whichever `job_id`/`entity_id` it was first written under. That's correct for
avoiding the LLM call, but `GET /jobs/{job_id}/entities` and
`.../entities/{entity_id}` (entities.py) both join `knowledge_summaries` on
`job_id` *and* `entity_id` scoped to the job being queried — so a re-ingested
job's unchanged entity, despite genuinely having been "summarized" via the
cache, had no `knowledge_summaries` row of its own for those endpoints to
find, and would render as if it were never summarized at all.
`copy_cached_summary_forward` closes that gap: still zero LLM/embedding cost,
but now one INSERT per cache hit, so every job's entities are queryable
through the same endpoints an uncached entity's summary is.
"""

from typing import Callable

from openai import AzureOpenAI
from supabase import Client

from document_analyzer.supabase_client import insert_scoped
from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.persist_summary import persist_summary
from document_analyzer.worker.summarize import summarize_entity


def lookup_cached_summary(client: Client, org_id: str, content_hash: str) -> dict | None:
    """The cached `knowledge_summaries` row for this (org_id, content_hash), or None on
    a cache miss (content never summarized before, or changed since its last hash)."""
    cache_rows = (
        client.table("summary_cache")
        .select("summary_id")
        .eq("org_id", org_id)
        .eq("content_hash", content_hash)
        .execute()
        .data
    )
    if not cache_rows:
        return None

    summary_rows = (
        client.table("knowledge_summaries")
        .select("*")
        .eq("org_id", org_id)
        .eq("id", cache_rows[0]["summary_id"])
        .execute()
        .data
    )
    return summary_rows[0] if summary_rows else None


def write_through_cache(client: Client, org_id: str, content_hash: str, summary_id: str) -> None:
    """Records that `content_hash` now maps to `summary_id`, so the next ingestion with
    the same content skips re-summarizing it."""
    client.table("summary_cache").insert(
        {"org_id": org_id, "content_hash": content_hash, "summary_id": summary_id}
    ).execute()


def copy_cached_summary_forward(client: Client, job_id: str, org_id: str, entity_id: str, cached: dict) -> dict:
    """Writes a new `knowledge_summaries` row scoped to *this* job/entity for a cache
    hit, copying `summary_text`/`risk_level`/`embedding`/`grounding_status` straight
    from the cached row — no LLM call, no new embedding, and no re-grounding (the
    content is byte-for-byte identical to what was already graded, so the prior
    verdict still holds). `summary_cache` itself is left pointing at the original
    (first-seen) summary_id; this is a queryable copy for the current job, not a
    replacement cache entry."""
    row = {
        "job_id": job_id,
        "level": "entity",
        "entity_id": entity_id,
        "summary_text": cached["summary_text"],
        "risk_level": cached["risk_level"],
        "embedding": cached["embedding"],
        # design.md §2: NOT NULL, defaults to "unverified" — .get() covers a cached row
        # that (like this one might) never went through persist_grounding_status itself.
        "grounding_status": cached.get("grounding_status", "unverified"),
    }
    return insert_scoped(client, "knowledge_summaries", org_id, row).data[0]


def summarize_entity_cached(
    client: Client,
    job_id: str,
    org_id: str,
    entity: ExtractedEntity,
    entity_db_id: str,
    content_hash: str,
    llm_client: AzureOpenAI | None = None,
    embed: Callable[[str], list[float]] | None = None,
) -> dict:
    """Entity-level summarize, short-circuited by summary_cache. Returns the
    (copied-forward-or-freshly-written) `knowledge_summaries` row, always scoped to
    this job_id/entity_db_id either way."""
    cached = lookup_cached_summary(client, org_id, content_hash)
    if cached is not None:
        return copy_cached_summary_forward(client, job_id, org_id, entity_db_id, cached)

    entity_summary = summarize_entity(entity, client=llm_client)
    row = persist_summary(client, job_id, org_id, entity_summary, entity_id=entity_db_id, embed=embed)
    write_through_cache(client, org_id, content_hash, row["id"])
    return row
