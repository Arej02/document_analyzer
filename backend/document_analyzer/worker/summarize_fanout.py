"""Worker step: concurrency-capped fan-out for entity-level summarization —
tasks.md Phase 3 "Concurrency cap for parallel entity-summary fan-out: default
10, env-configurable".

specs.md NFR5: bottom-up summarization bounds the size of each individual LLM
call, but total call count still scales with entity count — large repos need
parallel fan-out at the entity-summary level, with a concurrency cap, not
sequential processing. This is the piece that actually fans `summarize_entity`
(summarize.py) out across a job's entities, bounded by at most
AGENT1_SUMMARY_CONCURRENCY concurrent in-flight LLM calls at once (a thread
pool — summarize_entity's Azure OpenAI sync client is I/O-bound and
documented thread-safe, so threads are enough; no asyncio rewrite needed).

Module/subsystem/repo-level fan-out isn't handled here on purpose: NFR5 calls
out fan-out specifically "at the entity-summary level" — the levels above it
have far fewer, dependent (must-wait-for-their-own-children) calls, so they
don't need the same treatment.

A single entity's SummarizationError propagates and aborts the whole batch —
this fan-out only bounds concurrency, it doesn't decide how to handle a bad
summary. Retrying / flagging `unverified` without blocking the rest of the
job is Phase 4's grounding-checker job (CLAUDE.md "Grounding checker failures
never block ingestion"), not this one.

Persisting results into `knowledge_summaries`, and actually wiring this into
the worker's fetch -> detect -> extract -> persist -> summarize pipeline, are
separate, not-yet-built tasks — this module only runs summarize_entity for a
batch of entities and returns their results in memory.
"""

import os
from concurrent.futures import ThreadPoolExecutor

from document_analyzer.shared.llm import LLMClient

from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.summarize import EntitySummary, get_azure_openai_client, summarize_entity

# Starting default per specs.md "Decided" section ("Concurrency cap for entity-summary
# fan-out: 10 concurrent, configurable via env — starting default, revisit after Phase 6
# load testing") — same pattern as resource_bounds.py's ingestion bounds.
SUMMARY_CONCURRENCY = int(os.environ.get("AGENT1_SUMMARY_CONCURRENCY", 10))


def summarize_entities(
    entities: list[ExtractedEntity],
    client: LLMClient | None = None,
    max_workers: int | None = None,
) -> list[EntitySummary]:
    """Runs summarize_entity for every entity concurrently, bounded by at most
    max_workers (default SUMMARY_CONCURRENCY) in-flight LLM calls at once. Returns
    summaries in the same order as `entities`."""
    if not entities:
        return []

    client = client or get_azure_openai_client()
    cap = max_workers if max_workers is not None else SUMMARY_CONCURRENCY

    with ThreadPoolExecutor(max_workers=cap) as executor:
        return list(executor.map(lambda entity: summarize_entity(entity, client=client), entities))
