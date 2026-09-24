"""Stage `order`, part 1: cluster dependency graph & cycles — design_agent2.md §7.

Consumes the in-memory `CanonicalGraph` (§3) and `ClusterResult` (§4) and:

1. **Collapses** every directed canonical edge `a -> b` (a calls b, or callback a
   binds hook b) with `unit(a) != unit(b)` into a unit edge `unit(a) -> unit(b)`
   ("depends on"), and likewise at phase level. Containment never appears here.
2. **Finds SCCs** with `networkx.strongly_connected_components`; any SCC of more
   than one cluster is a cycle.
3. **Applies the policy.** A unit SCC inside one phase whose combined size is
   <= `max_unit_size` is auto-merged into a new `kind = merged_scc` unit (its
   internal edges flagged `merged`, members re-pointed in `cluster_members`).
   Any other unit SCC, and every phase SCC, is flagged `needs_facade`.
4. **Condenses** the post-merge unit graph and the phase graph
   (`networkx.condensation`) into DAGs for part 2's ordering (§8).

Persists `cluster_edges` and the `merged_scc` clusters on the service-role
client. A re-run first re-points members back to their original units, then
deletes this job's `merged_scc` clusters and `cluster_edges`, so it never
duplicates (specs FR1).
"""

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import networkx as nx
from supabase import Client

from document_analyzer.agent2.worker.cluster import ClusterResult, Unit
from document_analyzer.agent2.worker.graph import CanonicalGraph, EdgeCounts
from document_analyzer.supabase_client import insert_many_scoped


@dataclass
class ClusterEdge:
    level: str  # unit | phase
    from_id: str  # depends on `to_id` (to migrates first)
    to_id: str
    call_count: int = 0
    hook_binding_count: int = 0
    weight: float = 0.0
    flag: str = "none"  # none | merged | needs_facade
    scc_id: int | None = None


@dataclass
class DependencyResult:
    # Post-merge units: every unit not swallowed by an auto-merge, plus the merged_scc units.
    units: list[Unit]
    # merged_scc unit id -> the original unit ids it replaces (the originals stay in `clusters`)
    merged_from: dict[str, list[str]]
    edges: list[ClusterEdge]
    # scc_id -> cluster ids, for every needs_facade SCC (unit and phase level)
    facade_sccs: dict[int, list[str]]
    # Condensation DAGs (networkx.condensation): node attr `members` = cluster ids
    unit_dag: nx.DiGraph
    phase_dag: nx.DiGraph
    stats: dict = field(default_factory=dict)

    @property
    def open_facade_flags(self) -> int:
        """§7: +1 per needs_facade SCC, the initial `migration_plans.open_facade_flags`."""
        return len(self.facade_sccs)


def _pair_weight(graph: CanonicalGraph, a: str, b: str) -> float:
    """The §3.5 weight of one canonical pair. Hub pairs have no clustering weight
    (§3.4), so they fall back to the structural term alone."""
    key = (a, b) if a < b else (b, a)
    if key in graph.weights:
        return graph.weights[key]
    back = graph.directed.get((b, a), EdgeCounts())
    fwd = graph.directed.get((a, b), EdgeCounts())
    return math.log1p(fwd.calls + fwd.hook_bindings + back.calls + back.hook_bindings)


def _collapse(graph: CanonicalGraph, cluster_of: dict[str, str], level: str) -> dict[tuple[str, str], ClusterEdge]:
    """§7 step 1: aggregates directed canonical edges into cluster edges."""
    edges: dict[tuple[str, str], ClusterEdge] = {}
    for (a, b), counts in sorted(graph.directed.items()):
        ca, cb = cluster_of.get(a), cluster_of.get(b)
        if ca is None or cb is None or ca == cb:
            continue
        edge = edges.setdefault((ca, cb), ClusterEdge(level=level, from_id=ca, to_id=cb))
        edge.call_count += counts.calls
        edge.hook_binding_count += counts.hook_bindings
        edge.weight += _pair_weight(graph, a, b)
    return edges


def _cycles(edges: dict[tuple[str, str], ClusterEdge], key_of: dict[str, str]) -> list[list[str]]:
    """§7 step 2: SCCs with more than one cluster, each sorted and the list sorted by
    content key (cluster ids are random uuids), so scc_ids are stable across runs."""
    g = nx.DiGraph(list(edges))
    sccs = [sorted(c, key=key_of.__getitem__) for c in nx.strongly_connected_components(g) if len(c) > 1]
    return sorted(sccs, key=lambda c: key_of[c[0]])


def _condense(nodes: list[str], edges: dict[tuple[str, str], ClusterEdge]) -> nx.DiGraph:
    """§7 step 4: the condensation DAG, so a topological sort always exists."""
    g = nx.DiGraph()
    g.add_nodes_from(nodes)
    g.add_edges_from(edges)
    return nx.condensation(g)


def build_dependencies(graph: CanonicalGraph, clusters: ClusterResult, params: dict) -> DependencyResult:
    """Pure §7 (no DB access). Same graph + clusters give the same edges, SCCs and flags."""
    cap = int(params["max_unit_size"])
    unit_by_id = {u.id: u for u in clusters.units}
    # Stable content keys: a unit's lowest canonical node (as in cluster.py).
    key_of = {u.id: u.nodes[0] for u in clusters.units}

    unit_of = {n: u.id for u in clusters.units for n in u.nodes}
    original_edges = _collapse(graph, unit_of, "unit")

    # Auto-merge (§7 step 3): same phase and combined size within the cap.
    merged_from: dict[str, list[str]] = {}
    merged_into: dict[str, str] = {}
    merged_units: list[Unit] = []
    for scc in _cycles(original_edges, key_of):
        members = [unit_by_id[uid] for uid in scc]
        if len({u.phase_id for u in members}) == 1 and sum(len(u.nodes) for u in members) <= cap:
            merged = Unit(
                id=str(uuid.uuid4()),
                kind="merged_scc",
                nodes=sorted(n for u in members for n in u.nodes),
                phase_id=members[0].phase_id,
            )
            merged_units.append(merged)
            merged_from[merged.id] = scc
            for uid in scc:
                merged_into[uid] = merged.id
            key_of[merged.id] = merged.nodes[0]

    units = [u for u in clusters.units if u.id not in merged_into] + merged_units
    unit_of = {n: u.id for u in units for n in u.nodes}
    unit_edges = _collapse(graph, unit_of, "unit")

    # The merged SCCs' internal edges stay between the originals, for audit.
    scc_id = 0
    internal: list[ClusterEdge] = []
    for merged_id, originals in merged_from.items():
        for (a, b), edge in original_edges.items():
            if a in originals and b in originals:
                edge.flag, edge.scc_id = "merged", scc_id
                internal.append(edge)
        scc_id += 1

    # Merging only contracts SCCs, so the remaining unit cycles are the non-mergeable ones.
    facade_sccs: dict[int, list[str]] = {}
    for scc in _cycles(unit_edges, key_of):
        facade_sccs[scc_id] = scc
        scc_id += 1

    phase_key = {p.id: min(key_of[uid] for uid in p.unit_ids) for p in clusters.phases if p.unit_ids}
    phase_of = {n: u.phase_id for u in units for n in u.nodes}
    phase_edges = _collapse(graph, phase_of, "phase")
    # Phase-level cycles are always needs_facade (§7 step 3).
    for scc in _cycles(phase_edges, phase_key):
        facade_sccs[scc_id] = scc
        scc_id += 1

    for sid, scc in facade_sccs.items():
        edges = phase_edges if scc[0] in phase_key else unit_edges
        members = set(scc)
        for (a, b), edge in edges.items():
            if a in members and b in members:
                edge.flag, edge.scc_id = "needs_facade", sid

    edges = sorted(
        [*internal, *unit_edges.values(), *phase_edges.values()],
        key=lambda e: (e.level, e.scc_id is None, e.scc_id or 0, e.from_id, e.to_id),
    )
    return DependencyResult(
        units=units,
        merged_from=merged_from,
        edges=edges,
        facade_sccs=facade_sccs,
        unit_dag=_condense([u.id for u in units], unit_edges),
        phase_dag=_condense([p.id for p in clusters.phases], phase_edges),
        stats={
            "unit_edges": len(unit_edges),
            "phase_edges": len(phase_edges),
            "merged_sccs": len(merged_from),
            "facade_sccs": len(facade_sccs),
            "merged_into": merged_into,
        },
    )


def _restore_merged_members(client: Client, planning_job: dict, clusters: ClusterResult) -> None:
    """Undoes an earlier run's re-pointing, so deleting the merged_scc clusters
    (which cascades to their members) doesn't lose any `cluster_members` rows."""
    planning_job_id, org_id = planning_job["id"], planning_job["org_id"]
    merged = (
        client.table("clusters")
        .select("id")
        .eq("planning_job_id", planning_job_id)
        .eq("org_id", org_id)
        .eq("kind", "merged_scc")
        .execute()
        .data
    )
    merged_ids = [row["id"] for row in merged]
    if not merged_ids:
        return

    # Merged SCCs are within `max_unit_size`, so these reads are small.
    members = (
        client.table("cluster_members")
        .select("cluster_id, entity_id, canonical_entity_id")
        .in_("cluster_id", merged_ids)
        .eq("org_id", org_id)
        .execute()
        .data
    )
    unit_of = {n: u.id for u in clusters.units for n in u.nodes}
    restored = []
    for row in members:
        original = unit_of.get(row["canonical_entity_id"])
        if original is None:
            raise RuntimeError(
                f"merged_scc member {row['entity_id']} has no unit in this planning job's clusters; re-run stage cluster"
            )
        restored.append(
            {"cluster_id": original, "entity_id": row["entity_id"], "canonical_entity_id": row["canonical_entity_id"]}
        )

    client.table("clusters").delete().in_("id", merged_ids).eq("org_id", org_id).execute()
    if restored:
        insert_many_scoped(client, "cluster_members", org_id, restored)


def persist_dependencies(client: Client, planning_job: dict, clusters: ClusterResult, result: DependencyResult) -> None:
    planning_job_id, org_id = planning_job["id"], planning_job["org_id"]

    # Idempotent re-run (§7): restore and drop earlier merged_scc clusters, then this job's edges.
    _restore_merged_members(client, planning_job, clusters)
    client.table("cluster_edges").delete().eq("planning_job_id", planning_job_id).eq("org_id", org_id).execute()

    merged_units = [u for u in result.units if u.id in result.merged_from]
    if merged_units:
        insert_many_scoped(
            client,
            "clusters",
            org_id,
            [
                {
                    "id": u.id,
                    "planning_job_id": planning_job_id,
                    "level": "unit",
                    "parent_cluster_id": u.phase_id,
                    "kind": "merged_scc",
                    "size": len(u.nodes),
                }
                for u in merged_units
            ],
        )
        # Re-point members from the originals (kept for audit) to the merged unit.
        for u in merged_units:
            (
                client.table("cluster_members")
                .update({"cluster_id": u.id})
                .in_("cluster_id", result.merged_from[u.id])
                .eq("org_id", org_id)
                .execute()
            )

    if result.edges:
        insert_many_scoped(
            client,
            "cluster_edges",
            org_id,
            [
                {
                    "planning_job_id": planning_job_id,
                    "level": e.level,
                    "from_cluster_id": e.from_id,
                    "to_cluster_id": e.to_id,
                    "call_count": e.call_count,
                    "hook_binding_count": e.hook_binding_count,
                    "weight": e.weight,
                    "flag": e.flag,
                    "scc_id": e.scc_id,
                }
                for e in result.edges
            ],
        )


def run_dependencies_stage(
    client: Client, planning_job: dict, graph: CanonicalGraph, clusters: ClusterResult
) -> DependencyResult:
    """§7: builds and persists the cluster dependency graph, and adds edge/SCC counts
    and `merged_into` to `graph_stats`. Part 2 (§8) orders the returned DAGs."""
    result = build_dependencies(graph, clusters, planning_job["params"])
    persist_dependencies(client, planning_job, clusters, result)

    current = (
        client.table("planning_jobs")
        .select("graph_stats")
        .eq("id", planning_job["id"])
        .eq("org_id", planning_job["org_id"])
        .limit(1)
        .execute()
        .data
    )
    stats = {**((current[0].get("graph_stats") if current else None) or {}), **result.stats}
    (
        client.table("planning_jobs")
        .update({"graph_stats": stats})
        .eq("id", planning_job["id"])
        .eq("org_id", planning_job["org_id"])
        .execute()
    )
    return result
