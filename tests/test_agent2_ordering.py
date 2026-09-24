"""Tests for stage `order`, part 2 (agent2/worker/ordering.py) — design_agent2.md §8.

Inputs go through the real stage-`graph` builder and part 1's
`build_dependencies`, over hand-built `ClusterResult`s.
"""

from document_analyzer.agent2.worker.cluster import ClusterResult, Phase, Unit
from document_analyzer.agent2.worker.dependencies import build_dependencies
from document_analyzer.agent2.worker.graph import build_graph
from document_analyzer.agent2.worker.ordering import build_order, run_ordering_stage

from fakes import FakeClient

PARAMS = {"seed": 42, "alpha": 0.0, "max_unit_size": 25, "hub_percentile": 100, "leiden_resolution": 1.0}


def _entity(entity_id):
    return {"id": entity_id, "entity_type": "function", "name": entity_id, "file_path": "a.php", "raw_facts": {}}


def _edge(src, dst):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": "calls"}


def _graph(node_ids, edges):
    return build_graph([_entity(n) for n in node_ids], {}, edges, alpha=0.0, hub_percentile=100)


def _clusters(phases: dict[str, dict[str, list[str]]], foundation: str | None = None) -> ClusterResult:
    """{phase_id: {unit_id: [canonical nodes]}} -> ClusterResult; `foundation` names a
    phase whose single unit is the foundation unit."""
    units, phase_rows = [], []
    for phase_id, unit_map in phases.items():
        kind = "foundation" if phase_id == foundation else "community"
        phase_rows.append(Phase(id=phase_id, kind=kind, unit_ids=list(unit_map)))
        units.extend(Unit(id=uid, kind=kind, nodes=sorted(nodes), phase_id=phase_id) for uid, nodes in unit_map.items())
    return ClusterResult(units=units, phases=phase_rows, oversized_unit_ids=[])


def _order(node_ids, edges, clusters, risk=None, **params):
    deps = build_dependencies(_graph(node_ids, edges), clusters, {**PARAMS, **params})
    return build_order(clusters, deps, risk or {}), deps


def _positions(items):
    return {i.cluster_id: (i.phase_order, i.unit_order) for i in items}


def test_callee_is_ordered_before_its_caller():
    # Phase level: P1 calls into P2. Unit level: A calls B, though A's tie key is better.
    clusters = _clusters({"P1": {"A": ["a"], "B": ["b1", "b2"]}, "P2": {"C": ["c"]}})
    items, _ = _order(["a", "b1", "b2", "c"], [_edge("a", "b1"), _edge("b2", "c")], clusters, risk={"B": "high"})

    pos = _positions(items)
    assert pos["C"] < pos["B"] < pos["A"]
    assert pos == {"C": (1, 1), "B": (2, 1), "A": (2, 2)}


def test_foundation_is_first():
    clusters = _clusters({"F": {"FU": ["f1", "f2", "f3"]}, "P1": {"A": ["a"]}, "P2": {"B": ["b"]}}, foundation="F")
    # The foundation is high risk and the largest, so only "foundation first" puts it there.
    items, _ = _order(["f1", "f2", "f3", "a", "b"], [], clusters, risk={"FU": "high", "F": "high"})

    assert items[0].cluster_id == "FU" and items[0].phase_order == 1


def test_ties_go_low_risk_first_then_smaller_first():
    clusters = _clusters({"P": {"H": ["h"], "L2": ["l1", "l2"], "L1": ["l3"], "M": ["m"]}})
    items, _ = _order(["h", "l1", "l2", "l3", "m"], [], clusters, risk={"H": "high", "M": "medium"})

    assert [i.cluster_id for i in items] == ["L1", "L2", "M", "H"]


def test_order_is_deterministic():
    nodes = ["a", "b", "c", "d", "e"]
    edges = [_edge("a", "b"), _edge("c", "d"), _edge("e", "a")]
    clusters = _clusters({"P1": {"A": ["a"], "B": ["b"]}, "P2": {"C": ["c"], "D": ["d"], "E": ["e"]}})

    first, _ = _order(nodes, edges, clusters)
    second, _ = _order(list(reversed(nodes)), list(reversed(edges)), clusters)

    assert first == second


def test_flagged_scc_members_get_consecutive_unit_order():
    # A <-> C is a cycle too big to merge; B and D would otherwise sort between them.
    clusters = _clusters({"P": {"A": ["a1", "a2"], "B": ["b"], "C": ["c1", "c2"], "D": ["d"]}})
    items, deps = _order(
        ["a1", "a2", "b", "c1", "c2", "d"],
        [_edge("a1", "c1"), _edge("c2", "a2")],
        clusters,
        risk={"A": "low", "C": "low", "B": "medium", "D": "low"},
        max_unit_size=3,
    )

    assert list(deps.facade_sccs.values()) == [["A", "C"]]
    order = {i.cluster_id: i.unit_order for i in items}
    assert abs(order["A"] - order["C"]) == 1
    assert sorted(order.values()) == [1, 2, 3, 4]


def test_merged_unit_is_ordered_with_its_originals_rolled_up_risk():
    # A <-> B merges; its rolled-up risk (high, from B) puts it after the low-risk C.
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"], "C": ["c"]}})
    items, deps = _order(["a", "b", "c"], [_edge("a", "b"), _edge("b", "a")], clusters, risk={"B": "high"})

    [merged_id] = deps.merged_from
    assert [i.cluster_id for i in items] == ["C", merged_id]


# --- persistence -------------------------------------------------------------


def _planning_job():
    return {"id": "pj-1", "org_id": "org-1", "project_id": "proj-1", "source_job_id": "job-1", "params": PARAMS}


def test_persist_writes_draft_plan_v1_and_items():
    clusters = _clusters({"P1": {"A": ["a"]}, "P2": {"B": ["b"]}})
    deps = build_dependencies(_graph(["a", "b"], [_edge("a", "b"), _edge("b", "a")]), clusters, PARAMS)
    client = FakeClient()

    plan_id = run_ordering_stage(client, _planning_job(), clusters, deps, {}, test_contracts={"A": {"hooks_bound": ["init"]}})

    [plan] = client.table("migration_plans").rows
    assert plan["id"] == plan_id
    assert {k: plan[k] for k in ("org_id", "project_id", "planning_job_id", "source_job_id", "version", "status")} == {
        "org_id": "org-1", "project_id": "proj-1", "planning_job_id": "pj-1", "source_job_id": "job-1",
        "version": 1, "status": "draft",
    }
    # A cross-phase cycle: one unit SCC plus one phase SCC (§7).
    assert plan["open_facade_flags"] == 2 == deps.open_facade_flags

    items = {i["cluster_id"]: i for i in client.table("migration_plan_items").rows}
    assert set(items) == {"A", "B"}
    assert all(i["plan_id"] == plan_id and i["org_id"] == "org-1" for i in items.values())
    assert items["A"]["phase_cluster_id"] == "P1" and items["B"]["phase_cluster_id"] == "P2"
    assert items["A"]["test_contract"] == {"hooks_bound": ["init"]} and items["B"]["test_contract"] == {}


def test_rerun_replaces_the_unfinished_v1_without_duplicates():
    clusters = _clusters({"P": {"A": ["a"], "B": ["b"]}})
    deps = build_dependencies(_graph(["a", "b"], [_edge("a", "b")]), clusters, PARAMS)
    client = FakeClient()
    client.table("migration_plans").insert({"id": "other", "org_id": "org-2", "planning_job_id": "pj-1", "version": 1}).execute()

    run_ordering_stage(client, _planning_job(), clusters, deps, {})
    plan_id = run_ordering_stage(client, _planning_job(), clusters, deps, {})

    plans = client.table("migration_plans").rows
    assert sorted(p["id"] for p in plans) == sorted(["other", plan_id])  # other org untouched
    items = client.table("migration_plan_items").rows
    assert len(items) == 2 and all(i["plan_id"] == plan_id for i in items)
