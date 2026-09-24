"""Tests for the test-contract builder (agent2/worker/test_contract.py) — design_agent2.md §10.

The graph comes from the real stage-`graph` builder; units are hand-built.
"""

import json

from document_analyzer.agent2.worker.cluster import Unit
from document_analyzer.agent2.worker.graph import build_graph
from document_analyzer.agent2.worker.test_contract import (
    SUMMARY_EXCERPT_CHARS,
    build_test_contracts,
    run_test_contract_stage,
)

from fakes import FakeClient


def _fn(entity_id, raw_facts=None, file_path="a.php", entity_type="function"):
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "name": entity_id,
        "file_path": file_path,
        "raw_facts": raw_facts if raw_facts is not None else {"params": []},
    }


def _edge(src, dst, edge_type="calls"):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": edge_type}


def _contracts(entities, edges, units: dict[str, list[str]], summaries=None):
    graph = build_graph(entities, {}, edges, alpha=0.0, hub_percentile=100)
    unit_list = [Unit(id=uid, kind="community", nodes=sorted(nodes), phase_id="P") for uid, nodes in units.items()]
    return build_test_contracts(graph, unit_list, summaries or {})


def test_public_interface_is_cross_unit_targets_and_hook_bound_members_with_params():
    entities = [
        _fn("api", {"params": [{"name": "$id"}], "class": "Cart"}),
        _fn("helper"),  # only called from inside its own unit
        _fn("cb"),  # a hook callback
        _fn("init", {"kinds": ["action"]}, entity_type="hook"),
        _fn("caller"),
    ]
    edges = [_edge("caller", "api"), _edge("api", "helper"), _edge("cb", "init", "hook_binding")]

    contract = _contracts(entities, edges, {"U": ["api", "helper", "cb"], "V": ["caller"], "H": ["init"]})["U"]

    assert contract["public_interface"] == [
        {"entity_id": "api", "name": "api", "kind": "method", "class": "Cart", "params": [{"name": "$id"}], "file_path": "a.php"},
        {"entity_id": "cb", "name": "cb", "kind": "function", "class": None, "params": [], "file_path": "a.php"},
    ]


def test_hook_entity_bound_from_another_unit_is_public_with_kind_hook():
    entities = [_fn("cb"), _fn("init", {"kinds": ["action"]}, entity_type="hook")]

    contract = _contracts(entities, [_edge("cb", "init", "hook_binding")], {"U": ["cb"], "H": ["init"]})["H"]

    assert [(p["entity_id"], p["kind"], p["params"]) for p in contract["public_interface"]] == [("init", "hook", [])]


def test_incoming_call_sites_count_calls_from_other_units_only():
    entities = [_fn("t"), _fn("x"), _fn("y"), _fn("inner")]
    edges = [_edge("x", "t"), _edge("x", "t"), _edge("x", "t"), _edge("y", "t"), _edge("inner", "t")]

    contract = _contracts(entities, edges, {"U": ["t", "inner"], "X": ["x"], "Y": ["y"]})["U"]

    assert contract["incoming_call_sites"] == [
        {"from_unit_id": "X", "from_entity_id": "x", "to_entity_id": "t", "call_count": 3},
        {"from_unit_id": "Y", "from_entity_id": "y", "to_entity_id": "t", "call_count": 1},
    ]


def test_outgoing_dependencies_aggregate_per_target_unit():
    entities = [_fn("a1"), _fn("a2"), _fn("b1"), _fn("b2"), _fn("c"), _fn("h", {}, entity_type="hook")]
    edges = [_edge("a1", "b1"), _edge("a1", "b1"), _edge("a2", "b2"), _edge("a1", "c"), _edge("a2", "h", "hook_binding")]

    contract = _contracts(entities, edges, {"A": ["a1", "a2"], "B": ["b1", "b2"], "C": ["c"], "H": ["h"]})["A"]

    assert contract["outgoing_dependencies"] == [
        {"to_unit_id": "B", "call_count": 3, "hook_binding_count": 0},
        {"to_unit_id": "C", "call_count": 1, "hook_binding_count": 0},
        {"to_unit_id": "H", "call_count": 0, "hook_binding_count": 1},
    ]


def test_external_calls_are_unresolved_raw_facts_calls():
    entities = [_fn("a", {"params": [], "calls": ["b", "wp_remote_get", "update_post_meta"]}), _fn("b")]

    contract = _contracts(entities, [_edge("a", "b")], {"U": ["a", "b"]})["U"]

    assert contract["external_calls"] == ["update_post_meta", "wp_remote_get"]


def test_hooks_bound_names_the_hooks_members_bind_to():
    entities = [
        _fn("cb1"),
        _fn("cb2"),
        _fn("init", {}, entity_type="hook"),
        _fn("save_post", {}, entity_type="hook"),
    ]
    edges = [_edge("cb1", "init", "hook_binding"), _edge("cb2", "save_post", "hook_binding"), _edge("cb1", "save_post", "hook_binding")]

    contracts = _contracts(entities, edges, {"U": ["cb1", "cb2"], "H": ["init", "save_post"]})

    assert contracts["U"]["hooks_bound"] == ["init", "save_post"]
    assert contracts["H"]["hooks_bound"] == []  # being bound to isn't binding


def test_side_effect_signals_are_medium_and_high_risk_members_with_an_excerpt():
    entities = [_fn("a"), _fn("b"), _fn("c"), _fn("d")]
    summaries = {
        "a": {"entity_id": "a", "risk_level": "high", "summary_text": "x" * 500, "grounding_status": "verified"},
        "b": {"entity_id": "b", "risk_level": "medium", "summary_text": "writes post meta", "grounding_status": "verified"},
        "c": {"entity_id": "c", "risk_level": "low", "summary_text": "pure", "grounding_status": "verified"},
    }

    contract = _contracts(entities, [], {"U": ["a", "b", "c", "d"]}, summaries)["U"]

    assert contract["side_effect_signals"] == [
        {"entity_id": "a", "risk_level": "high", "summary_excerpt": "x" * SUMMARY_EXCERPT_CHARS},
        {"entity_id": "b", "risk_level": "medium", "summary_excerpt": "writes post meta"},
    ]


def test_unverified_member_ids_includes_a_duplicates_summary():
    # "dup" collapses onto "a" (same file, name, type); only the duplicate has a summary.
    entities = [_fn("a"), {**_fn("dup"), "name": "a", "raw_facts": {"role": "hook_callback"}}, _fn("b")]
    summaries = {
        "dup": {"entity_id": "dup", "risk_level": "low", "summary_text": "s", "grounding_status": "unverified"},
        "b": {"entity_id": "b", "risk_level": "low", "summary_text": "s", "grounding_status": "verified"},
    }

    contract = _contracts(entities, [], {"U": ["a", "b"]}, summaries)["U"]

    assert contract["unverified_member_ids"] == ["a"]


def test_merged_cluster_ids_is_empty_and_contract_is_json_serializable():
    contracts = _contracts([_fn("a"), _fn("b")], [_edge("a", "b")], {"U": ["a"], "V": ["b"]})

    assert all(c["merged_cluster_ids"] == [] for c in contracts.values())
    json.dumps(contracts)  # stored as jsonb


def test_contract_is_deterministic():
    entities = [_fn("a", {"params": [], "calls": ["z", "y"]}), _fn("b"), _fn("c")]
    edges = [_edge("b", "a"), _edge("c", "a"), _edge("a", "c")]
    units = {"U": ["a"], "V": ["b", "c"]}

    assert _contracts(entities, edges, units) == _contracts(list(reversed(entities)), list(reversed(edges)), units)


def test_run_stage_loads_org_scoped_entity_summaries():
    client = FakeClient()
    table = client.table("knowledge_summaries")
    for org_id, grounding in (("org-1", "unverified"), ("org-2", "verified")):
        table.insert(
            {"entity_id": "a", "job_id": "job-1", "org_id": org_id, "level": "entity", "risk_level": "low",
             "summary_text": "s", "grounding_status": grounding}
        ).execute()
    graph = build_graph([_fn("a")], {}, [], alpha=0.0, hub_percentile=100)
    planning_job = {"id": "pj-1", "org_id": "org-1", "source_job_id": "job-1"}

    contracts = run_test_contract_stage(client, planning_job, graph, [Unit(id="U", kind="community", nodes=["a"], phase_id="P")])

    assert contracts["U"]["unverified_member_ids"] == ["a"]
