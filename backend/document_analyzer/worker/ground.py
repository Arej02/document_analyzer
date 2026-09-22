"""Worker step: grounding checker — tasks.md Phase 4
"Grounding checker: validate each summary against underlying facts".

Validates one already-generated summary (summary_text, risk_level, risk_factors —
any of EntitySummary/ModuleSummary/SubsystemSummary/RepoSummary's shape, from
summarize.py/summarize_module.py/summarize_subsystem.py) against the same facts
payload its summarizer call was scoped to: an entity's own `raw_facts`
(summarize.py's `_entity_facts_payload`) at the entity level, or a module's/
subsystem's/repo's children summaries (summarize_module.py's
`_module_facts_payload` / summarize_subsystem.py's `_children_payload`) at every
level above it. This module doesn't care which — it takes whatever JSON-shaped
`facts` the caller passed to the summarizer and checks the summary against it,
via a single LLM call scoped only to that same facts payload (specs.md FR5's
"validates each generated summary against its underlying facts").

A grounding check is orthogonal to summarization quality: it can find that a
summary invented a claim the facts don't support, or that it under-reported a
risk signal that IS present in the facts (understating risk_level is treated
as an ungrounded claim, not just a quality nit — an enterprise reviewer relying
on risk_level to prioritize review needs it to not be silently wrong).

Not built here — separate, not-yet-built Phase 4 tasks:
- The retry loop (regenerate the summary and re-check, up to a cap of 2).
- Flagging `grounding_status = unverified` in `knowledge_summaries` once the
  retry cap is exhausted, and `verified` on a grounded result.
- The job only reaching `status = done` once every summary is verified/unverified.
- Cascading re-summarization of ancestor summaries when an entity summary changes.

This module is a pure function over its inputs: one LLM call in, one verdict
out, no Supabase access, same shape as summarize.py.
"""

import json
import os
from dataclasses import dataclass
from typing import Literal

from openai import AzureOpenAI

from document_analyzer.worker.summarize import RiskLevel, get_azure_openai_client

GroundingVerdict = Literal["grounded", "ungrounded"]

_SYSTEM_PROMPT = """You are fact-checking a summary generated for a legacy-codebase migration \
knowledge base, against the exact facts it was generated from. You are given "facts" (the JSON \
payload the summary was produced from) and "generated_summary" (the summary text, risk_level, \
and risk_factors that were produced from those facts alone).

Decide whether generated_summary is fully GROUNDED in facts:
- Every claim in the summary text must be directly supported by facts — no invented behavior, \
no detail about code/files/entities not present in facts.
- risk_level must not understate what facts actually support: if facts contain a risk signal \
(e.g. a dynamic code execution call, a shell call, a credential marker, a raw SQL concatenation, \
a file-system write, an outbound network call) that the summary/risk_level ignores or downplays, \
that is an ungrounded claim.
- You are checking for unsupported or omitted claims, not re-deriving the "textbook correct" \
risk_level from scratch — a defensible risk_level that fairly reflects facts is grounded even if \
you might have picked differently.

Respond with a single JSON object and nothing else:
{"grounded": true | false, "issues": ["..."]}

`issues` must name each specific unsupported or omitted claim found (short phrases, e.g. \
"summary omits the shell_exec call present in facts.raw_facts.calls"). Leave it empty only when \
grounded is true.
"""


class GroundingCheckError(Exception):
    """Raised when the LLM call fails, or its response doesn't match the expected shape.

    Distinct from an *ungrounded* GroundingResult: that's a successful check that found a
    real problem with the summary. This is the check itself being unusable (bad JSON, a
    missing/non-bool `grounded`) — the not-yet-built retry loop needs to tell those apart,
    since only the latter is a summary-quality signal.
    """


@dataclass(frozen=True)
class GroundingResult:
    verdict: GroundingVerdict
    issues: list[str]


def _grounding_payload(facts: dict, summary_text: str, risk_level: RiskLevel, risk_factors: list[str]) -> dict:
    return {
        "facts": facts,
        "generated_summary": {
            "summary": summary_text,
            "risk_level": risk_level,
            "risk_factors": risk_factors,
        },
    }


def check_grounding(
    facts: dict,
    summary_text: str,
    risk_level: RiskLevel,
    risk_factors: list[str],
    client: AzureOpenAI | None = None,
) -> GroundingResult:
    """Checks one already-generated summary against the same `facts` payload its
    summarizer call was scoped to. Level-agnostic: works for an entity's raw_facts
    payload or a module/subsystem/repo's children-summaries payload alike, since both
    are just JSON the caller already built for the summarizer call. One LLM call,
    independent of repo size and of any retry/persistence logic (see module docstring).
    """
    client = client or get_azure_openai_client()
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    response = client.chat.completions.create(
        model=deployment,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(_grounding_payload(facts, summary_text, risk_level, risk_factors)),
            },
        ],
    )

    content = response.choices[0].message.content
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GroundingCheckError(f"LLM response was not valid JSON: {content!r}") from exc

    grounded = parsed.get("grounded")
    issues = parsed.get("issues", [])

    if not isinstance(grounded, bool):
        raise GroundingCheckError(f"LLM response had a non-bool 'grounded': {grounded!r}")
    if not isinstance(issues, list):
        raise GroundingCheckError(f"LLM response had a non-list 'issues': {issues!r}")

    return GroundingResult(verdict="grounded" if grounded else "ungrounded", issues=issues)
