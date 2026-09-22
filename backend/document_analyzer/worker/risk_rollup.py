"""Shared deterministic risk_level rollup for every summarization level above
entity — tasks.md Phase 3 (module, subsystem, repo).

See design.md §Risk level heuristic ("module level and up") for why this
replaces a fresh LLM judgment call: once entities are folded into a summary,
their own raw_facts are gone from the payload (only summary_text travels
upward, per specs.md FR4's "small regardless of repo size"), so there is
nothing left for the entity-level rubric to fact-check against. Instead every
level above entity takes the deterministic maximum of its children's already
fact-checked risk_level, with risk_factors naming which child/children
carried it. `summarize_module.py`, `summarize_subsystem.py`, and the
not-yet-built repo level all share this one rollup rather than each
reimplementing it.
"""

from document_analyzer.worker.summarize import RiskLevel

_RISK_ORDER: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}


def rollup_risk(named_levels: list[tuple[str, RiskLevel]]) -> tuple[RiskLevel, list[str]]:
    """Highest risk_level among `named_levels`, plus the name(s) that carried it.
    Empty input defaults to ("low", []) — a level with no children has nothing to be
    risky about."""
    if not named_levels:
        return "low", []

    highest_level: RiskLevel = max((level for _, level in named_levels), key=lambda level: _RISK_ORDER[level])
    names = [name for name, level in named_levels if level == highest_level]
    return highest_level, names
