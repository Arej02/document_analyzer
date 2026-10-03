"""Worker step: module-level summarization — tasks.md Phase 3
"Module-level summarization (entity summaries -> module summary)".

Folds one module's entity summaries into a single module-level summary. A
"module" is one file: every entity sharing the same `extracted_entities.file_path`
(design.md §2) belongs to the same module — no other module concept exists
anywhere in the data model. Per specs.md FR4, the LLM call here sees only
this module's entities' own already-generated summaries — never raw_facts,
never another module's entities — keeping the call small regardless of total
repo size, the same property that makes summarize_entity's fan-out possible.

risk_level is deliberately NOT re-judged by the LLM at this level. Re-running
the entity rubric (summarize.py) against already-summarized text — which has
lost the raw_facts detail the rubric actually matches against — would be a
strictly less grounded signal than what already exists: each constituent
entity already carries a fact-checked risk_level. So a module's risk_level is
the deterministic maximum of its entities' risk_levels (low < medium < high),
and risk_factors names which entity/entities drove that level. This keeps
the module level explainable per FR4 the same way the entity rubric does,
just via a different (non-LLM) mechanism. Documented here and mirrored in
design.md §Risk level heuristic — keep both in sync.

Subsystem/repo-level summarization (same fold, one level up), the
concurrency-capped fan-out that produces the entity summaries this consumes,
and persisting into `knowledge_summaries` are separate, not-yet-built Phase 3
tasks — this module only produces one module's summary in memory; it never
touches Supabase.
"""

import json
import os
from dataclasses import dataclass

from document_analyzer.shared.llm import LLMClient

from document_analyzer.worker.extract import EntityType
from document_analyzer.worker.risk_rollup import rollup_risk
from document_analyzer.worker.summarize import (
    EntitySummary,
    RiskLevel,
    SummarizationError,
    get_azure_openai_client,
)

_SYSTEM_PROMPT = """You are summarizing one module (a single file) of a legacy PHP/WordPress \
codebase, for a migration knowledge base. You are given ONLY the already-generated summaries \
of the entities (functions, classes, hooks, schema registrations, routes, dependencies) \
extracted from this one file — no raw code, no other modules. Do not assume anything about \
the rest of the codebase that isn't implied by these summaries.

Write a concise (2-4 sentence) plain-English summary of what this module/file does as a whole, \
synthesizing its entities' summaries.

Respond with a single JSON object and nothing else:
{"summary": "..."}
"""


@dataclass(frozen=True)
class ModuleEntitySummary:
    """One constituent entity, identified by name/type, plus its own already-generated
    summary — the only thing this level's LLM call and risk rollup are allowed to see."""

    entity_name: str
    entity_type: EntityType
    summary: EntitySummary


@dataclass(frozen=True)
class ModuleSummary:
    summary_text: str
    risk_level: RiskLevel
    risk_factors: list[str]


def compute_module_risk(entity_summaries: list[ModuleEntitySummary]) -> tuple[RiskLevel, list[str]]:
    """Deterministic rollup: the highest risk_level among constituent entities, with
    risk_factors naming which entity/entities drove it. See module docstring for why this
    is a rollup rather than a fresh LLM judgment call. Delegates to the shared
    risk_rollup.rollup_risk used by every level above entity."""
    return rollup_risk([(es.entity_name, es.summary.risk_level) for es in entity_summaries])


def _module_facts_payload(file_path: str, entity_summaries: list[ModuleEntitySummary]) -> dict:
    """Only entity name/type/summary_text — no raw_facts, no risk_level (the rollup above
    decides risk_level deterministically, not the LLM)."""
    return {
        "file_path": file_path,
        "entities": [
            {
                "name": es.entity_name,
                "entity_type": es.entity_type,
                "summary": es.summary.summary_text,
            }
            for es in entity_summaries
        ],
    }


def summarize_module(
    file_path: str,
    entity_summaries: list[ModuleEntitySummary],
    client: LLMClient | None = None,
) -> ModuleSummary:
    """Folds one module's (already-generated) entity summaries into a module summary.
    One LLM call, independent of repo size, scoped to only this module's entities."""
    if not entity_summaries:
        raise ValueError("summarize_module requires at least one entity summary")

    client = client or get_azure_openai_client()
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    response = client.chat.completions.create(
        model=deployment,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_module_facts_payload(file_path, entity_summaries))},
        ],
    )

    content = response.choices[0].message.content
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SummarizationError(f"LLM response was not valid JSON: {content!r}") from exc

    summary_text = parsed.get("summary")
    if not isinstance(summary_text, str) or not summary_text.strip():
        raise SummarizationError(f"LLM response missing a non-empty 'summary': {parsed!r}")

    risk_level, risk_factors = compute_module_risk(entity_summaries)

    return ModuleSummary(summary_text=summary_text.strip(), risk_level=risk_level, risk_factors=risk_factors)
