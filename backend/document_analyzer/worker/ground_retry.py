"""Worker step: grounding retry loop — tasks.md Phase 4
"Retry loop up to a defined cap: 2 retries (starting default — revisit if
false-`unverified` rate is too high in practice)".

specs.md FR5: "On failure: regenerate, up to a retry cap." Wraps ground.py's
`check_grounding` around a caller-supplied `generate` callable that produces a
fresh summary attempt (a new LLM call, not a re-check of the same text) — if
the first attempt comes back ungrounded, `generate` is called again and the
new attempt is re-checked, up to `GROUNDING_RETRY_CAP` (default 2, per
specs.md's "Decided" section — same env-configurable-with-a-default pattern as
summarize_fanout.py's SUMMARY_CONCURRENCY) additional regenerations.

Deliberately decoupled from which level is being summarized: `generate` is
supplied by the caller as a zero-argument closure (e.g. `lambda:
summarize_entity(entity, client=client)` at the entity level, or the
equivalent closure over summarize_module.py/summarize_subsystem.py at higher
levels) — this module never imports a concrete summarizer, mirroring
ground.py's own level-agnostic design. `facts` is the same JSON payload
`check_grounding` expects: whatever the caller already built for the
summarizer call being retried.

Not built here — separate, not-yet-built Phase 4 tasks:
- Flagging `grounding_status = unverified` in `knowledge_summaries` once the
  retry cap is exhausted (`verified` on a grounded result) — this module
  returns the final verdict in memory but never touches Supabase.
- The job only reaching `status = done` once every summary is verified/unverified.
- Cascading re-summarization of ancestor summaries when an entity summary changes.
"""

import os
from dataclasses import dataclass
from typing import Callable, Protocol

from document_analyzer.shared.llm import LLMClient

from document_analyzer.worker.ground import GroundingResult, check_grounding
from document_analyzer.worker.summarize import RiskLevel, get_azure_openai_client

# Starting default per specs.md "Decided" section ("Grounding checker retry cap: 2
# retries before flagging unverified — starting default, revisit if false-unverified
# rate is too high in practice") — same env-configurable-with-a-default pattern as
# summarize_fanout.py's SUMMARY_CONCURRENCY.
GROUNDING_RETRY_CAP = int(os.environ.get("AGENT1_GROUNDING_RETRY_CAP", 2))


class SummaryLike(Protocol):
    """The only shape this module needs from a generated summary — matches
    EntitySummary/ModuleSummary/SubsystemSummary/RepoSummary's common fields without
    importing any concrete summarizer (see module docstring)."""

    summary_text: str
    risk_level: RiskLevel
    risk_factors: list[str]


@dataclass(frozen=True)
class GroundingRetryResult:
    summary: SummaryLike
    grounding: GroundingResult
    retries_used: int


def check_grounding_with_retry(
    facts: dict,
    generate: Callable[[], SummaryLike],
    max_retries: int | None = None,
    client: LLMClient | None = None,
) -> GroundingRetryResult:
    """Generates a summary via `generate()`, checks it against `facts`, and — while the
    verdict is "ungrounded" and the cap isn't exhausted — calls `generate()` again for a
    fresh attempt and re-checks. Returns the last attempt's summary and grounding
    verdict regardless of outcome (a still-ungrounded result after the cap is not an
    error here — flagging `unverified` is the next, separate task); `retries_used`
    counts regenerations actually performed (0 when the first attempt is grounded).
    """
    cap = max_retries if max_retries is not None else GROUNDING_RETRY_CAP
    client = client or get_azure_openai_client()

    summary = generate()
    grounding = check_grounding(facts, summary.summary_text, summary.risk_level, summary.risk_factors, client=client)

    retries_used = 0
    while grounding.verdict == "ungrounded" and retries_used < cap:
        summary = generate()
        grounding = check_grounding(
            facts, summary.summary_text, summary.risk_level, summary.risk_factors, client=client
        )
        retries_used += 1

    return GroundingRetryResult(summary=summary, grounding=grounding, retries_used=retries_used)
