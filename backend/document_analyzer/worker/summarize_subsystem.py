"""Worker step: subsystem/repo-level summarization — tasks.md Phase 3
"Subsystem/repo-level summarization, same pattern".

One level up from summarize_module.py, twice: a subsystem summary folds its
modules' summaries, and the single repo summary folds every subsystem
summary — never straight from modules to repo (specs.md FR4: "Subsystem/repo
summaries generated the same way, one level up at a time"). Both folds are
the exact same shape as the module level — a named child (name, summary_text,
risk_level) folded into a parent summary via one LLM call scoped only to its
children's own summaries (never raw_facts, never a sibling's children), plus
the shared deterministic risk_level rollup (risk_rollup.py) rather than a
fresh LLM judgment — so both are implemented here via one shared
`_summarize_children` helper with two thin, level-named wrappers
(`summarize_subsystem`, `summarize_repo`).

Subsystem grouping (decided, mirrors the module = one file decision in
design.md §2): a subsystem is the top-level directory component of its
modules' `file_path` — e.g. "includes/checkout.php" and "includes/cart.php"
both belong to subsystem "includes". A file with no directory component
(sitting at repo root) belongs to the sentinel subsystem "(root)". No other
subsystem concept exists in the data model. `subsystem_for_file()` below is
the single source of truth for this grouping.

Persisting into `knowledge_summaries`, and the concurrency-capped fan-out
that actually groups modules into subsystems (via `subsystem_for_file`) and
subsystems into the repo, are separate, not-yet-built Phase 3 tasks — this
module only produces one summary in memory per call; it never touches
Supabase.
"""

import json
import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from openai import AzureOpenAI

from document_analyzer.worker.risk_rollup import rollup_risk
from document_analyzer.worker.summarize import RiskLevel, SummarizationError, get_azure_openai_client

_ROOT_SUBSYSTEM = "(root)"

_SYSTEM_PROMPT_TEMPLATE = """You are summarizing one {level} of a legacy PHP/WordPress \
codebase, for a migration knowledge base. You are given ONLY the already-generated \
summaries of this {level}'s {child_level}s — no raw code, no facts below that level. Do \
not assume anything about the rest of the codebase that isn't implied by these summaries.

Write a concise (2-4 sentence) plain-English summary of what this {level} does as a whole, \
synthesizing its {child_level}s' summaries.

Respond with a single JSON object and nothing else:
{{"summary": "..."}}
"""


@dataclass(frozen=True)
class ChildSummary:
    """One constituent child (a module, when folding a subsystem; a subsystem, when
    folding the repo), identified by name, plus its own already-generated
    summary_text/risk_level — the only thing the parent-level LLM call and risk rollup
    are allowed to see."""

    name: str
    summary_text: str
    risk_level: RiskLevel


@dataclass(frozen=True)
class SubsystemSummary:
    summary_text: str
    risk_level: RiskLevel
    risk_factors: list[str]


@dataclass(frozen=True)
class RepoSummary:
    summary_text: str
    risk_level: RiskLevel
    risk_factors: list[str]


def subsystem_for_file(file_path: str) -> str:
    """The subsystem a module (file) belongs to: its top-level directory component, or
    the "(root)" sentinel for a file with no directory component. See module docstring."""
    parts = PurePosixPath(file_path).parts
    return parts[0] if len(parts) > 1 else _ROOT_SUBSYSTEM


def _children_payload(name: str, children: list[ChildSummary]) -> dict:
    return {"name": name, "children": [{"name": c.name, "summary": c.summary_text} for c in children]}


def _summarize_children(
    name: str,
    level: str,
    child_level: str,
    children: list[ChildSummary],
    client: AzureOpenAI | None,
) -> tuple[str, RiskLevel, list[str]]:
    """Shared fold: one LLM call for summary_text (scoped to only `children`'s own
    summaries) plus the shared deterministic risk_level rollup. Returns raw fields so
    each level-named wrapper can return its own dataclass type."""
    if not children:
        raise ValueError(f"summarize_{level} requires at least one {child_level} summary")

    client = client or get_azure_openai_client()
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    response = client.chat.completions.create(
        model=deployment,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(level=level, child_level=child_level)},
            {"role": "user", "content": json.dumps(_children_payload(name, children))},
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

    risk_level, risk_factors = rollup_risk([(c.name, c.risk_level) for c in children])

    return summary_text.strip(), risk_level, risk_factors


def summarize_subsystem(
    subsystem_name: str, module_summaries: list[ChildSummary], client: AzureOpenAI | None = None
) -> SubsystemSummary:
    """Folds one subsystem's (already-generated) module summaries into a subsystem
    summary. One LLM call, scoped only to this subsystem's modules."""
    summary_text, risk_level, risk_factors = _summarize_children(
        subsystem_name, "subsystem", "module", module_summaries, client
    )
    return SubsystemSummary(summary_text=summary_text, risk_level=risk_level, risk_factors=risk_factors)


def summarize_repo(subsystem_summaries: list[ChildSummary], client: AzureOpenAI | None = None) -> RepoSummary:
    """Folds every subsystem summary into the single repo-level summary. One LLM call,
    scoped only to subsystem summaries — never skips straight from modules to repo
    (specs.md FR4)."""
    summary_text, risk_level, risk_factors = _summarize_children(
        "repo", "repo", "subsystem", subsystem_summaries, client
    )
    return RepoSummary(summary_text=summary_text, risk_level=risk_level, risk_factors=risk_factors)
