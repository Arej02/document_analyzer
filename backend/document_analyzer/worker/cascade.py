"""Worker step: cascading re-summarization — tasks.md Phase 4
"Cascading re-summarization: when an entity summary is (re)generated, mark its
ancestor module/subsystem/repo summaries stale and regenerate them too — never
leave a parent summary built on outdated children" (specs.md FR6, cross-
referenced from Phase 4 since it's the last piece needed for the knowledge
base to stay internally consistent after any (re-)summarization).

Reuses Phase 3's per-level summarizers as-is (summarize_module.py,
summarize_subsystem.py) — this module's only job is ordering: when one
entity's summary changes, its module must be regenerated from its *current*
(already-updated) full set of entity summaries, its subsystem must then be
regenerated from that *freshly regenerated* module summary (never the stale
one) alongside its other, already-current sibling modules, and the repo must
then be regenerated from that *freshly regenerated* subsystem summary
alongside its other, already-current sibling subsystems. Skipping straight
from module to repo, or folding a stale module/subsystem into its parent,
would leave exactly the inconsistency FR6 rules out.

`module_entity_summaries` (the changed module's full, current entity set) and
the sibling module/subsystem summaries are supplied by the caller rather than
queried here, for the same reason summarize_module.py/summarize_subsystem.py
themselves take their children as plain arguments rather than querying
Supabase: keeps this a pure function, independent of storage. Loading those
current values from `knowledge_summaries` and replacing the old module/
subsystem/repo rows once regenerated is separate, not-yet-built wiring — and
it runs into a real gap worth flagging rather than silently working around:
`knowledge_summaries` (design.md §2) has no column identifying *which*
module/subsystem a level=`module`/`subsystem` row is for (only `entity_id`,
nullable, which is null at those levels) — there is currently no query that
can find "the existing module summary row for file X" to replace. That's a
schema question for whoever wires this up, not addressed here.
"""

from dataclasses import dataclass

from document_analyzer.shared.llm import LLMClient

from document_analyzer.worker.summarize_module import ModuleEntitySummary, ModuleSummary, summarize_module
from document_analyzer.worker.summarize_subsystem import (
    ChildSummary,
    RepoSummary,
    SubsystemSummary,
    subsystem_for_file,
    summarize_repo,
    summarize_subsystem,
)


@dataclass(frozen=True)
class CascadeResult:
    file_path: str
    subsystem_name: str
    module_summary: ModuleSummary
    subsystem_summary: SubsystemSummary
    repo_summary: RepoSummary


def regenerate_ancestors(
    file_path: str,
    module_entity_summaries: list[ModuleEntitySummary],
    sibling_module_summaries: list[ChildSummary],
    sibling_subsystem_summaries: list[ChildSummary],
    client: LLMClient | None = None,
) -> CascadeResult:
    """Regenerates every ancestor of a changed entity — its module, that module's
    subsystem, and the repo — in that order, always folding the just-regenerated child
    into the next step rather than a stale one.

    - `module_entity_summaries`: the changed module's *full* current entity-summary set
      (design.md §2: a module = one file) — summarize_module always sees a module's
      complete children, not a diff.
    - `sibling_module_summaries`: every other already-current module in the same
      subsystem (i.e. excluding `file_path`'s own module, which this call regenerates).
    - `sibling_subsystem_summaries`: every other already-current subsystem in the repo
      (i.e. excluding this file's own subsystem, which this call regenerates).
    """
    subsystem_name = subsystem_for_file(file_path)

    module_summary = summarize_module(file_path, module_entity_summaries, client=client)

    fresh_module = ChildSummary(
        name=file_path, summary_text=module_summary.summary_text, risk_level=module_summary.risk_level
    )
    subsystem_summary = summarize_subsystem(
        subsystem_name, [fresh_module, *sibling_module_summaries], client=client
    )

    fresh_subsystem = ChildSummary(
        name=subsystem_name, summary_text=subsystem_summary.summary_text, risk_level=subsystem_summary.risk_level
    )
    repo_summary = summarize_repo([fresh_subsystem, *sibling_subsystem_summaries], client=client)

    return CascadeResult(
        file_path=file_path,
        subsystem_name=subsystem_name,
        module_summary=module_summary,
        subsystem_summary=subsystem_summary,
        repo_summary=repo_summary,
    )
