"""Which units' reports/data stand in for a plan item — shared by Agent 2's and
Agent 3's read paths (design_agent2.md §7, §9.2; design_agent3.md §4 step 1).

A plan item's own `clusters` row has no `cluster_reports` when it's a
`merged_scc` unit (§7, the originals were contracted into it) or an override
merge (§9.2, the synthetic row is never given its own report). In both cases
the constituent original units' reports stand in. Pure (no DB access), so it's
importable by Agent 3 without either agent calling into the other's router or
worker (CLAUDE.md "Importing a *pure* helper function ... is allowed and isn't
an agent call"). Moved here from agent2/router.py when Agent 3 needed the same
rule for its unit context (design_agent3.md §4).
"""


def unit_sources(item: dict, merged_from: dict[str, list[str]]) -> list[str]:
    """The units whose reports stand for this plan item: the override-merged units
    (§9.2), a merged_scc unit's originals (§7), or just the item's own cluster. An
    override merge that took in a merged_scc unit stands on that unit's originals.

    `merged_from` is merged_scc unit id -> its original unit ids, inverted from
    `planning_jobs.graph_stats.merged_into` (§7) — the caller loads that.
    """
    unit_id = item["cluster_id"]
    override_merged = (item.get("test_contract") or {}).get("merged_cluster_ids") or []
    if override_merged:
        return [source for merged in override_merged for source in merged_from.get(merged, [merged])]
    return merged_from.get(unit_id) or [unit_id]
