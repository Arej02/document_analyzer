"""Test-contract builder — design_agent2.md §10 (specs FR8).

Builds each unit's `migration_plan_items.test_contract` from Agent 1 facts
only: the canonical graph (§3), the post-merge units (§7) and the source
job's entity summaries. No LLM call is made. Nothing here writes: stage
`order` part 2 (`ordering.py`) persists the contracts with the plan items.

Agent 3 decides how to turn a contract into tests. Agent 2 never generates
or runs any.
"""

from collections import defaultdict

from supabase import Client

from document_analyzer.agent2.worker.cluster import Unit
from document_analyzer.agent2.worker.graph import CanonicalGraph, _fetch_all
from document_analyzer.agent2.worker.report import external_call_names

# Entity risk levels that count as a side-effect signal (`low` doesn't).
SIDE_EFFECT_RISK_LEVELS = ("medium", "high")

# `side_effect_signals[].summary_excerpt` length, in characters.
SUMMARY_EXCERPT_CHARS = 200


def load_entity_summaries(client: Client, source_job_id: str, org_id: str) -> dict[str, dict]:
    """Entity-level summaries for `source_job_id`, keyed by entity id (read-only, org-scoped)."""
    rows = _fetch_all(
        lambda: client.table("knowledge_summaries")
        .select("id, entity_id, summary_text, risk_level, grounding_status")
        .eq("job_id", source_job_id)
        .eq("org_id", org_id)
        .eq("level", "entity")
    )
    return {row["entity_id"]: row for row in rows if row.get("entity_id")}


def _summary(graph: CanonicalGraph, summaries: dict[str, dict], node_id: str) -> dict | None:
    """The representative's summary, else any duplicate's (§3.2)."""
    for entity_id in [node_id, *graph.members.get(node_id, [])]:
        if entity_id in summaries:
            return summaries[entity_id]
    return None


def _kind(node: dict) -> str:
    raw_facts = node.get("raw_facts") or {}
    if node["entity_type"] == "hook":
        return "hook"
    if node["entity_type"] == "function" and raw_facts.get("class"):
        return "method"
    return node["entity_type"]


def build_test_contract(
    graph: CanonicalGraph, unit: Unit, unit_of: dict[str, str], summaries: dict[str, dict]
) -> dict:
    """§10 contract for one unit. `unit_of` maps every canonical node to its
    (post-merge) unit id. Every list is sorted, so the same facts give the same contract."""
    members = set(unit.nodes)

    incoming: list[dict] = []
    outgoing: dict[str, dict] = {}
    # Members at either end of a "binding" edge (hook_binding or route_binding —
    # graph.py counts both into EdgeCounts.hook_bindings, since a route binds to
    # its controller method the same way a callback binds to a hook).
    hook_bound: set[str] = set()
    hooks: set[str] = set()
    routes: set[str] = set()
    cross_unit_targets: set[str] = set()
    for (a, b), counts in sorted(graph.directed.items()):
        if counts.hook_bindings and (a in members or b in members):
            hook_bound.update(n for n in (a, b) if n in members)
            if a in members:
                target_type = graph.nodes[b]["entity_type"]
                (routes if target_type == "route" else hooks).add(graph.nodes[b]["name"])
        if b in members and a not in members:
            cross_unit_targets.add(b)
            if counts.calls:
                incoming.append(
                    {"from_unit_id": unit_of[a], "from_entity_id": a, "to_entity_id": b, "call_count": counts.calls}
                )
        elif a in members and b not in members:
            dep = outgoing.setdefault(unit_of[b], {"to_unit_id": unit_of[b], "call_count": 0, "hook_binding_count": 0})
            dep["call_count"] += counts.calls
            dep["hook_binding_count"] += counts.hook_bindings

    public_interface = []
    for node_id in sorted(cross_unit_targets | hook_bound):
        node = graph.nodes[node_id]
        raw_facts = node.get("raw_facts") or {}
        public_interface.append(
            {
                "entity_id": node_id,
                "name": node["name"],
                "kind": _kind(node),
                "class": raw_facts.get("class"),
                "params": raw_facts.get("params") or [],
                "file_path": node["file_path"],
            }
        )

    side_effect_signals = []
    unverified = []
    for node_id in sorted(members):
        summary = _summary(graph, summaries, node_id)
        if summary is None:
            continue
        if summary.get("risk_level") in SIDE_EFFECT_RISK_LEVELS:
            side_effect_signals.append(
                {
                    "entity_id": node_id,
                    "risk_level": summary["risk_level"],
                    "summary_excerpt": (summary.get("summary_text") or "")[:SUMMARY_EXCERPT_CHARS],
                }
            )
        if summary.get("grounding_status") == "unverified":
            unverified.append(node_id)

    return {
        "public_interface": public_interface,
        "incoming_call_sites": incoming,
        "outgoing_dependencies": [outgoing[k] for k in sorted(outgoing)],
        "external_calls": external_call_names(graph, sorted(members)),
        "hooks_bound": sorted(hooks),
        "routes_bound": sorted(routes),
        "side_effect_signals": side_effect_signals,
        "unverified_member_ids": unverified,
        # Filled only by override merges (§9.2); the worker never sets it.
        "merged_cluster_ids": [],
    }


def build_test_contracts(graph: CanonicalGraph, units: list[Unit], summaries: dict[str, dict]) -> dict[str, dict]:
    """Contracts for every (post-merge) unit, keyed by unit id."""
    unit_of = {n: u.id for u in units for n in u.nodes}
    return {u.id: build_test_contract(graph, u, unit_of, summaries) for u in units}


def run_test_contract_stage(
    client: Client, planning_job: dict, graph: CanonicalGraph, units: list[Unit]
) -> dict[str, dict]:
    """Loads the source job's entity summaries and builds every unit's contract.
    `units` are part 1's post-merge units (`DependencyResult.units`)."""
    summaries = load_entity_summaries(client, planning_job["source_job_id"], planning_job["org_id"])
    return build_test_contracts(graph, units, summaries)
