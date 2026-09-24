"""Stage `order`, part 2: ordering & plan emission — design_agent2.md §8.

Consumes part 1's `DependencyResult` (§7) plus each cluster's report risk and:

1. Topologically sorts the phase condensation DAG with
   `networkx.lexicographical_topological_sort`, dependencies first, breaking
   ties by `(foundation first, risk low->high, size, content key)`. This gives
   `phase_order`.
2. Within each phase, sorts the condensed unit sub-DAG (edges between that
   phase's units only) the same way. A flagged SCC is one condensation node,
   so its members get consecutive `unit_order` values. This gives `unit_order`.
3. Writes `migration_plans` version 1 (`draft`, `open_facade_flags` from §7)
   and one `migration_plan_items` row per post-merge unit.

Both orders are 1-based. The last tie-break is a cluster's lowest canonical
entity id rather than its (random) uuid, so the order is the same across runs.
"""

from dataclasses import dataclass

import networkx as nx
from supabase import Client

from document_analyzer.agent2.worker.cluster import ClusterResult, Unit
from document_analyzer.agent2.worker.dependencies import DependencyResult
from document_analyzer.supabase_client import insert_many_scoped
from document_analyzer.worker.risk_rollup import rollup_risk

_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class PlanItem:
    cluster_id: str
    phase_cluster_id: str
    phase_order: int
    unit_order: int


@dataclass(frozen=True)
class _Key:
    foundation: bool
    risk: str
    size: int
    content: str

    def sort_key(self) -> tuple:
        return (not self.foundation, _RISK_RANK[self.risk], self.size, self.content)


def _combine(keys: list[_Key]) -> _Key:
    """Tie key of a condensation node (an SCC): foundation if any member is, the
    highest member risk, the total size and the lowest content key."""
    risk, _ = rollup_risk([(k.content, k.risk) for k in keys])
    return _Key(any(k.foundation for k in keys), risk, sum(k.size for k in keys), min(k.content for k in keys))


def _sorted_members(dag: nx.DiGraph, keys: dict[str, _Key]) -> list[str]:
    """Dependencies first (`dag` edges run dependent -> dependency), then members of
    each condensation node, all by tie key."""
    node_keys = {n: _combine([keys[m] for m in dag.nodes[n]["members"]]).sort_key() for n in dag}
    order = []
    for node in nx.lexicographical_topological_sort(dag.reverse(copy=True), key=node_keys.__getitem__):
        order.extend(sorted(dag.nodes[node]["members"], key=lambda m: keys[m].sort_key()))
    return order


def _unit_keys(deps: DependencyResult, risk_by_cluster: dict[str, str]) -> dict[str, _Key]:
    keys = {}
    for unit in deps.units:
        # A merged_scc unit has no report of its own (§7): roll up its originals'.
        sources = deps.merged_from.get(unit.id, [unit.id])
        risk, _ = rollup_risk([(uid, risk_by_cluster.get(uid, "low")) for uid in sources])
        keys[unit.id] = _Key(unit.kind == "foundation", risk, len(unit.nodes), unit.nodes[0])
    return keys


def build_order(clusters: ClusterResult, deps: DependencyResult, risk_by_cluster: dict[str, str]) -> list[PlanItem]:
    """Pure §8 steps 1-2 (no DB access). Returns plan items in (phase_order, unit_order)."""
    unit_keys = _unit_keys(deps, risk_by_cluster)
    units_by_phase: dict[str, list[Unit]] = {p.id: [] for p in clusters.phases}
    for unit in deps.units:
        units_by_phase[unit.phase_id].append(unit)

    phase_keys = {}
    for phase in clusters.phases:
        members = [unit_keys[u.id] for u in units_by_phase[phase.id]]
        risk = risk_by_cluster.get(phase.id) or rollup_risk([(k.content, k.risk) for k in members])[0]
        phase_keys[phase.id] = _Key(
            phase.kind == "foundation", risk, sum(k.size for k in members), min((k.content for k in members), default="")
        )

    items: list[PlanItem] = []
    for phase_order, phase_id in enumerate(_sorted_members(deps.phase_dag, phase_keys), start=1):
        unit_ids = {u.id for u in units_by_phase[phase_id]}
        sub = nx.DiGraph()
        sub.add_nodes_from(sorted(unit_ids))
        sub.add_edges_from(
            (e.from_id, e.to_id)
            for e in deps.edges
            if e.level == "unit" and e.flag != "merged" and e.from_id in unit_ids and e.to_id in unit_ids
        )
        for unit_order, unit_id in enumerate(_sorted_members(nx.condensation(sub), unit_keys), start=1):
            items.append(PlanItem(unit_id, phase_id, phase_order, unit_order))
    return items


def delete_unfinished_plan(client: Client, planning_job: dict) -> None:
    """A re-run of this (not yet `done`) planning job replaces a version 1 an earlier
    attempt left behind. It was never handed out: `plan_id` stays null until `done`."""
    org_id = planning_job["org_id"]
    existing = (
        client.table("migration_plans")
        .select("id")
        .eq("planning_job_id", planning_job["id"])
        .eq("org_id", org_id)
        .eq("version", 1)
        .execute()
        .data
    )
    for row in existing:
        client.table("migration_plan_items").delete().eq("plan_id", row["id"]).eq("org_id", org_id).execute()
        client.table("migration_plans").delete().eq("id", row["id"]).eq("org_id", org_id).execute()


def persist_plan(
    client: Client,
    planning_job: dict,
    items: list[PlanItem],
    open_facade_flags: int,
    test_contracts: dict[str, dict] | None = None,
) -> str:
    """Inserts `migration_plans` v1 (`draft`) and its items. Returns the plan id."""
    org_id = planning_job["org_id"]
    delete_unfinished_plan(client, planning_job)

    plan = insert_many_scoped(
        client,
        "migration_plans",
        org_id,
        [
            {
                "project_id": planning_job["project_id"],
                "planning_job_id": planning_job["id"],
                "source_job_id": planning_job["source_job_id"],
                "version": 1,
                "parent_plan_id": None,
                "status": "draft",
                "open_facade_flags": open_facade_flags,
            }
        ],
    ).data[0]
    contracts = test_contracts or {}
    if items:
        insert_many_scoped(
            client,
            "migration_plan_items",
            org_id,
            [
                {
                    "plan_id": plan["id"],
                    "cluster_id": item.cluster_id,
                    "phase_cluster_id": item.phase_cluster_id,
                    "phase_order": item.phase_order,
                    "unit_order": item.unit_order,
                    "test_contract": contracts.get(item.cluster_id, {}),
                }
                for item in items
            ],
        )
    return plan["id"]


def run_ordering_stage(
    client: Client,
    planning_job: dict,
    clusters: ClusterResult,
    deps: DependencyResult,
    risk_by_cluster: dict[str, str],
    test_contracts: dict[str, dict] | None = None,
) -> str:
    """§8: orders phases and units and writes plan version 1. `risk_by_cluster` is
    each unit's and phase's `cluster_reports.risk_level`; `test_contracts` (§10) is
    keyed by unit id. Returns the plan id."""
    items = build_order(clusters, deps, risk_by_cluster)
    return persist_plan(client, planning_job, items, deps.open_facade_flags, test_contracts)
