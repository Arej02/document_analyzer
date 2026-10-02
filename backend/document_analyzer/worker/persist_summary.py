"""Worker step: persist summarization output — tasks.md Phase 3
"Write results to `knowledge_summaries` with `risk_level` and `embedding`".

Runs on the async worker only, via the service-role client (bypasses RLS) —
same write pattern as persist.py's extracted_entities/entity_edges. Takes one
already-generated summary (EntitySummary from summarize.py, ModuleSummary
from summarize_module.py, or SubsystemSummary/RepoSummary from
summarize_subsystem.py) and writes it into `knowledge_summaries` (design.md
§2), stamping org_id from the job, never from ingested content, per
CLAUDE.md's cross-tenant-leak warning for worker writes. `level` is inferred
from the summary's own type rather than taken as a separate argument, so a
caller can't pass a mismatched (summary, level) pair by mistake.

Every summary's `embedding` is generated here, from its own summary_text,
using the pinned local model (embed.py) — never sent to Azure OpenAI, per
design.md's embedding-model-stays-local decision.

`grounding_status` is left at its schema default (`unverified`) on every
insert: design.md §1's pipeline writes summaries as a *draft* first
("Bottom-up LLM summarization -> knowledge_summaries (draft)"), and only
Phase 4's not-yet-built grounding checker ever flips a row to `verified`.
This module never sets grounding_status itself.

`entity_id` is required for an EntitySummary and forbidden otherwise, per the
knowledge_summaries schema note ("entity_id ... null for module/subsystem/
repo level") — enforced here rather than left to a caller mistake.

`summary_cache` write-through (keyed by content_hash) is a separate,
not-yet-built task directly after this one.
"""

from typing import Callable, Literal

from supabase import Client

from document_analyzer.supabase_client import insert_scoped
from document_analyzer.worker.embed import embed_text
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleSummary
from document_analyzer.worker.summarize_subsystem import RepoSummary, SubsystemSummary

SummaryLevel = Literal["entity", "module", "subsystem", "repo"]
AnySummary = EntitySummary | ModuleSummary | SubsystemSummary | RepoSummary

_LEVEL_BY_TYPE: dict[type, SummaryLevel] = {
    EntitySummary: "entity",
    ModuleSummary: "module",
    SubsystemSummary: "subsystem",
    RepoSummary: "repo",
}


def persist_summary(
    client: Client,
    job_id: str,
    org_id: str,
    summary: AnySummary,
    entity_id: str | None = None,
    embed: Callable[[str], list[float]] | None = None,
) -> dict:
    """Embeds `summary.summary_text` with the pinned local model and inserts one
    `knowledge_summaries` row. `embed` is injectable (default embed_text) so callers/
    tests can avoid loading the real embedding model. Returns the inserted row."""
    level = _LEVEL_BY_TYPE[type(summary)]
    if level == "entity" and entity_id is None:
        raise ValueError("entity_id is required when persisting an EntitySummary")
    if level != "entity" and entity_id is not None:
        raise ValueError(
            f"entity_id must be None when persisting a {type(summary).__name__} "
            "(design.md §2: null for module/subsystem/repo level)"
        )

    embed = embed or embed_text
    embedding = embed(summary.summary_text)

    row = {
        "job_id": job_id,
        "level": level,
        "entity_id": entity_id,
        "summary_text": summary.summary_text,
        "risk_level": summary.risk_level,
        "risk_factors": list(summary.risk_factors),
        "embedding": embedding,
    }
    return insert_scoped(client, "knowledge_summaries", org_id, row).data[0]
