"""Tests for stage `order`, part 1 (agent2/worker/dependencies.py) — design_agent2.md §7.

The canonical graph comes from the real stage-`graph` builder; the units and
phases are hand-built `ClusterResult`s so each test controls exactly which
units share a phase and how big they are.
"""

import networkx as nx

from document_analyzer.agent2.worker.cluster import ClusterResult, Phase, Unit
from document_analyzer.agent2.worker.dependencies import build_dependencies, run_dependencies_stage
from document_analyzer.agent2.worker.graph import build_graph

from fakes import FakeClient

PARAMS = {"seed": 42, "alpha": 0.0, "max_unit_size": 25, "hub_percentile": 100, "leiden_resolution": 1.0}


def _entity(entity_id):
    return {"id": entity_id, "entity_type": "function", "name": entity_id, "file_path": "a.php", "raw_facts": {}}


def _edge(src, dst, edge_type="calls"):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": edge_type}


def _graph(node_ids, edges):
    return build_graph([_entity(n) for n in node_ids], {}, edges, alpha=0.0, hub_percentile=100)


def _clusters(phases: dict[str, dict[str, list[str]]]) -> ClusterResult:
    """{phase_id: {unit_id: [canonical nodes]}} -> ClusterResult."""
    units, phase_rows = [], []
    for phase_id, unit_map in phases.items():
        phase_rows.append(Phase(id=phase_id, kind="community", unit_ids=list(unit_map)))
        units.extend(Unit(id=uid, kind="community", nodes=sorted(nodes), phase_id=phase_id) for uid, nodes in unit_map.items())
    return ClusterResult(units=units, phases=phase_rows, oversized_unit_ids=[])


def _edges(result, level, flag=None):
    return [e for e in result.edges if e.level == level and (flag is None or e.flag == flag)]


def test_collapse_counts_calls_and_hook_bindings_and_skips_intra_unit_edges():
    g = _graph(["a1", "a2", "b1"], [_edge("a1", "b1"), _edge("a1", "b1"), _edge("a2", "b1", "hook_binding"), _edge("a1", "a2")])
    clusters = _clusters({"P": {"A": ["a1", "a2"], "B": ["b1"]}})

    result = build_dependencies(g, clusters, PARAMS)

    [edge] = _edges(result, "unit")
    assert (edge.from_id, edge.to_id) == ("A", "B")  # A depends on B
    assert (edge.call_count, edge.hook_binding_count, edge.flag) == (2, 1, "none")
    assert edge.weight > 0
    assert _edges(result, "phase") == []  # both units in one phase
    assert result.open_facade_flags == 0


def test_two_cycle_is_detected():
    g = _graph(["a", "b"], [_edge("a", "b"), _edge("b", "a")])
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"]}})

    result = build_dependencies(g, clusters, {**PARAMS, "max_unit_size": 1})  # too big to merge

    assert list(result.facade_sccs.values()) == [["A", "B"]]
    flagged = _edges(result, "unit", "needs_facade")
    assert {(e.from_id, e.to_id) for e in flagged} == {("A", "B"), ("B", "A")}
    assert len({e.scc_id for e in flagged}) == 1


def test_three_cycle_is_detected():
    g = _graph(["a", "b", "c", "d"], [_edge("a", "b"), _edge("b", "c"), _edge("c", "a"), _edge("c", "d")])
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"], "C": ["c"], "D": ["d"]}})

    result = build_dependencies(g, clusters, {**PARAMS, "max_unit_size": 2})

    assert list(result.facade_sccs.values()) == [["A", "B", "C"]]
    # The edge out of the cycle isn't part of it.
    out = next(e for e in result.edges if (e.from_id, e.to_id) == ("C", "D"))
    assert out.flag == "none" and out.scc_id is None


def test_mergeable_scc_is_merged():
    g = _graph(["a", "b", "c"], [_edge("a", "b"), _edge("b", "a"), _edge("c", "a")])
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"], "C": ["c"]}})

    result = build_dependencies(g, clusters, PARAMS)

    [(merged_id, originals)] = result.merged_from.items()
    assert originals == ["A", "B"]
    merged = next(u for u in result.units if u.id == merged_id)
    assert (merged.kind, merged.nodes, merged.phase_id) == ("merged_scc", ["a", "b"], "P")
    assert {u.id for u in result.units} == {merged_id, "C"}
    # Internal edges stay between the originals, flagged merged; the rest point at the merged unit.
    assert {(e.from_id, e.to_id) for e in _edges(result, "unit", "merged")} == {("A", "B"), ("B", "A")}
    assert {(e.from_id, e.to_id) for e in _edges(result, "unit", "none")} == {("C", merged_id)}
    assert result.facade_sccs == {} and result.open_facade_flags == 0
    assert result.stats["merged_into"] == {"A": merged_id, "B": merged_id}


def test_oversized_scc_is_flagged_not_merged():
    g = _graph(["a1", "a2", "b1", "b2"], [_edge("a1", "b1"), _edge("b2", "a2")])
    clusters = _clusters({"P": {"A": ["a1", "a2"], "B": ["b1", "b2"]}})

    result = build_dependencies(g, clusters, {**PARAMS, "max_unit_size": 3})  # combined 4 > 3

    assert result.merged_from == {}
    assert list(result.facade_sccs.values()) == [["A", "B"]]
    assert result.open_facade_flags == 1


def test_cross_phase_scc_is_flagged_at_unit_and_phase_level():
    g = _graph(["a", "b"], [_edge("a", "b"), _edge("b", "a")])
    clusters = _clusters({"P1": {"A": ["a"]}, "P2": {"B": ["b"]}})

    result = build_dependencies(g, clusters, PARAMS)  # small enough, but not in one phase

    assert result.merged_from == {}
    assert sorted(result.facade_sccs.values()) == [["A", "B"], ["P1", "P2"]]
    assert {e.flag for e in _edges(result, "unit")} == {"needs_facade"}
    assert {e.flag for e in _edges(result, "phase")} == {"needs_facade"}
    assert result.open_facade_flags == 2


def test_phase_cycle_without_unit_cycle_is_flagged():
    # A -> C and D -> B: no unit cycle, but P1 <-> P2 at phase level.
    g = _graph(["a", "b", "c", "d"], [_edge("a", "c"), _edge("d", "b")])
    clusters = _clusters({"P1": {"A": ["a"], "B": ["b"]}, "P2": {"C": ["c"], "D": ["d"]}})

    result = build_dependencies(g, clusters, PARAMS)

    assert list(result.facade_sccs.values()) == [["P1", "P2"]]
    assert {e.flag for e in _edges(result, "unit")} == {"none"}


def test_condensation_is_a_dag_with_the_flagged_scc_as_one_node():
    g = _graph(["a", "b", "c"], [_edge("a", "b"), _edge("b", "a"), _edge("c", "a")])
    clusters = _clusters({"P1": {"A": ["a"], "C": ["c"]}, "P2": {"B": ["b"]}})

    result = build_dependencies(g, clusters, PARAMS)

    assert nx.is_directed_acyclic_graph(result.unit_dag)
    assert nx.is_directed_acyclic_graph(result.phase_dag)
    members = sorted(sorted(result.unit_dag.nodes[n]["members"]) for n in result.unit_dag)
    assert members == [["A", "B"], ["C"]]


def test_scc_ids_and_flags_are_deterministic():
    edges = [_edge("a", "b"), _edge("b", "a"), _edge("c", "d"), _edge("d", "c")]
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"], "C": ["c"], "D": ["d"]}})

    first = build_dependencies(_graph(["a", "b", "c", "d"], edges), clusters, {**PARAMS, "max_unit_size": 1})
    second = build_dependencies(_graph(["d", "c", "b", "a"], list(reversed(edges))), clusters, {**PARAMS, "max_unit_size": 1})

    assert first.facade_sccs == second.facade_sccs == {0: ["A", "B"], 1: ["C", "D"]}
    assert [(e.from_id, e.to_id, e.flag, e.scc_id) for e in first.edges] == [
        (e.from_id, e.to_id, e.flag, e.scc_id) for e in second.edges
    ]


# --- persistence -------------------------------------------------------------


def _seeded_client(clusters: ClusterResult, graph, org_id="org-1"):
    client = FakeClient()
    client.cascades["clusters"] = [("cluster_members", "cluster_id"), ("cluster_edges", "from_cluster_id")]
    client.table("planning_jobs").insert({"id": "pj-1", "org_id": org_id, "graph_stats": {"nodes": 3}}).execute()
    for phase in clusters.phases:
        client.table("clusters").insert(
            {"id": phase.id, "org_id": org_id, "planning_job_id": "pj-1", "level": "phase", "kind": "community"}
        ).execute()
    for unit in clusters.units:
        client.table("clusters").insert(
            {"id": unit.id, "org_id": org_id, "planning_job_id": "pj-1", "level": "unit", "kind": unit.kind,
             "parent_cluster_id": unit.phase_id, "size": len(unit.nodes)}
        ).execute()
        for n in unit.nodes:
            for entity_id in graph.members[n]:
                client.table("cluster_members").insert(
                    {"cluster_id": unit.id, "entity_id": entity_id, "canonical_entity_id": n, "org_id": org_id}
                ).execute()
    return client


def _planning_job():
    return {"id": "pj-1", "org_id": "org-1", "params": PARAMS}


def test_persist_writes_merged_cluster_repoints_members_and_writes_edges():
    g = _graph(["a", "b", "c"], [_edge("a", "b"), _edge("b", "a"), _edge("c", "a")])
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"], "C": ["c"]}})
    client = _seeded_client(clusters, g)

    result = run_dependencies_stage(client, _planning_job(), g, clusters)

    [merged_id] = result.merged_from
    merged_row = next(c for c in client.table("clusters").rows if c["id"] == merged_id)
    assert (merged_row["kind"], merged_row["parent_cluster_id"], merged_row["size"], merged_row["org_id"]) == (
        "merged_scc", "P", 2, "org-1"
    )
    # Originals are kept for audit; their members now point at the merged unit.
    assert {"A", "B"} <= {c["id"] for c in client.table("clusters").rows}
    holder = {m["entity_id"]: m["cluster_id"] for m in client.table("cluster_members").rows}
    assert holder == {"a": merged_id, "b": merged_id, "c": "C"}

    edges = client.table("cluster_edges").rows
    assert len(edges) == len(result.edges) == 3
    assert all(e["org_id"] == "org-1" and e["planning_job_id"] == "pj-1" for e in edges)
    stats = client.table("planning_jobs").rows[0]["graph_stats"]
    assert stats["nodes"] == 3 and stats["merged_sccs"] == 1 and stats["merged_into"] == {"A": merged_id, "B": merged_id}


def test_rerun_deletes_and_rewrites_merged_clusters_and_edges():
    g = _graph(["a", "b", "c", "d"], [_edge("a", "b"), _edge("b", "a"), _edge("c", "d"), _edge("d", "c")])
    clusters = _clusters({"P1": {"A": ["a"], "B": ["b"]}, "P2": {"C": ["c"]}, "P3": {"D": ["d"]}})
    client = _seeded_client(clusters, g)

    first = run_dependencies_stage(client, _planning_job(), g, clusters)
    second = run_dependencies_stage(client, _planning_job(), g, clusters)

    merged_rows = [c for c in client.table("clusters").rows if c["kind"] == "merged_scc"]
    assert [c["id"] for c in merged_rows] == list(second.merged_from)
    assert list(first.merged_from)[0] not in {c["id"] for c in client.table("clusters").rows}
    # Every member survives exactly once, pointing at the current merged unit.
    members = client.table("cluster_members").rows
    assert sorted(m["entity_id"] for m in members) == ["a", "b", "c", "d"]
    [merged_id] = second.merged_from
    assert {m["entity_id"]: m["cluster_id"] for m in members}["a"] == merged_id
    edges = client.table("cluster_edges").rows
    assert len(edges) == len(second.edges)
    assert sorted((e["flag"], e["scc_id"]) for e in edges) == sorted((e.flag, e.scc_id) for e in second.edges)


def test_rerun_delete_is_scoped_by_org():
    g = _graph(["a", "b"], [_edge("a", "b")])
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"]}})
    client = _seeded_client(clusters, g)
    client.table("cluster_edges").insert({"id": "other", "org_id": "org-2", "planning_job_id": "pj-1"}).execute()

    run_dependencies_stage(client, _planning_job(), g, clusters)

    assert any(e["id"] == "other" for e in client.table("cluster_edges").rows)
