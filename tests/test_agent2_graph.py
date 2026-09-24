"""Tests for stage `graph` (agent2/worker/graph.py) — design_agent2.md §3.

`build_graph` is exercised directly on in-memory rows; `run_graph_stage` runs
against the FakeClient to cover loading (org/job scoping, edge-type filtering,
paging) and the `graph_stats` write.
"""

import json
import math

import pytest

from document_analyzer.agent2.worker import graph as graph_module
from document_analyzer.agent2.worker.graph import build_graph, run_graph_stage

from fakes import FakeClient


def _entity(entity_id, name=None, file_path="a.php", entity_type="function", raw_facts=None):
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "name": name or entity_id,
        "file_path": file_path,
        "raw_facts": raw_facts if raw_facts is not None else {"params": []},
    }


def _edge(src, dst, edge_type="calls"):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": edge_type}


def _summary(entity_id, embedding, grounding_status="verified"):
    return {"entity_id": entity_id, "grounding_status": grounding_status, "embedding": embedding}


def _build(entities, edges, summaries=None, alpha=0.0, hub_percentile=99):
    return build_graph(entities, summaries or {}, edges, alpha=alpha, hub_percentile=hub_percentile)


def test_duplicate_collapse_rewires_edges():
    entities = [
        _entity("e1", name="init_cb", raw_facts={"params": [], "line": 3}),  # PhpExtractor's
        _entity("e2", name="init_cb", raw_facts={"role": "hook_callback"}),  # WordPressExtractor's
        _entity("hook", name="init", entity_type="hook", raw_facts={"kinds": ["action"]}),
        _entity("caller"),
    ]
    edges = [_edge("e2", "hook", "hook_binding"), _edge("caller", "e2")]

    g = _build(entities, edges)

    assert set(g.nodes) == {"e1", "hook", "caller"}
    assert g.members["e1"] == ["e1", "e2"]
    assert g.directed[("e1", "hook")].hook_bindings == 1
    assert g.directed[("caller", "e1")].calls == 1
    assert g.stats["canonicalized"] == 1


def test_representative_prefers_entity_with_summary():
    entities = [
        _entity("e1", name="cb", raw_facts={"params": []}),
        _entity("e2", name="cb", raw_facts={"role": "hook_callback"}),
    ]
    g = _build(entities, [], summaries={"e2": _summary("e2", None)})
    assert set(g.nodes) == {"e2"}


def test_same_method_name_in_two_classes_of_one_file_not_collapsed():
    entities = [
        _entity("A", entity_type="class", raw_facts={}),
        _entity("B", entity_type="class", raw_facts={}),
        _entity("a_ctor", name="__construct", raw_facts={"class": "A"}),
        _entity("b_ctor", name="__construct", raw_facts={"class": "B"}),
    ]
    g = _build(entities, [])
    assert {"a_ctor", "b_ctor"} <= set(g.nodes)
    assert g.stats["canonicalized"] == 0


def test_self_loop_after_collapse_is_dropped():
    entities = [_entity("e1", name="f"), _entity("e2", name="f", raw_facts={"role": "hook_callback"})]
    g = _build(entities, [_edge("e2", "e1")])
    assert g.directed == {}
    assert g.weights == {}


def test_call_count_equals_row_count():
    entities = [_entity("a"), _entity("b")]
    edges = [_edge("a", "b")] * 3 + [_edge("b", "a")]

    g = _build(entities, edges)

    assert g.directed[("a", "b")].calls == 3
    assert g.directed[("b", "a")].calls == 1
    assert g.stats["call_rows"] == 4
    assert g.weights[("a", "b")] == pytest.approx(math.log1p(4))


def test_alpha_zero_gives_structure_only_weights():
    entities = [_entity("a"), _entity("b")]
    summaries = {"a": _summary("a", [1.0, 0.0]), "b": _summary("b", [1.0, 0.0])}

    g = _build(entities, [_edge("a", "b")], summaries=summaries, alpha=0.0)

    assert g.weights[("a", "b")] == pytest.approx(math.log1p(1))


def test_similarity_term_added_when_both_verified():
    entities = [_entity("a"), _entity("b")]
    # pgvector's text form, as PostgREST returns it.
    summaries = {"a": _summary("a", "[1,0]"), "b": _summary("b", json.dumps([1, 1]))}

    g = _build(entities, [_edge("a", "b")], summaries=summaries, alpha=0.5)

    assert g.weights[("a", "b")] == pytest.approx(math.log1p(1) + 0.5 * (1 / math.sqrt(2)))


@pytest.mark.parametrize("b_summary", [None, "unverified"])
def test_unverified_or_missing_endpoint_drops_similarity(b_summary):
    entities = [_entity("a"), _entity("b")]
    summaries = {"a": _summary("a", [1.0, 0.0])}
    if b_summary:
        summaries["b"] = _summary("b", [1.0, 0.0], grounding_status=b_summary)

    g = _build(entities, [_edge("a", "b")], summaries=summaries, alpha=0.5)

    assert g.weights[("a", "b")] == pytest.approx(math.log1p(1))


def test_negative_similarity_is_clamped_to_zero():
    entities = [_entity("a"), _entity("b")]
    summaries = {"a": _summary("a", [1.0, 0.0]), "b": _summary("b", [-1.0, 0.0])}
    g = _build(entities, [_edge("a", "b")], summaries=summaries, alpha=0.5)
    assert g.weights[("a", "b")] == pytest.approx(math.log1p(1))


def test_containment_edge_links_class_and_methods_at_max_weight():
    entities = [
        _entity("C", name="Cart", entity_type="class", raw_facts={"methods": ["add"]}),
        _entity("m", name="add", raw_facts={"class": "Cart", "params": []}),
        _entity("other_file_m", name="add", file_path="b.php", raw_facts={"class": "Cart"}),
        _entity("x"),
        _entity("y"),
    ]
    g = _build(entities, [_edge("x", "y")] * 4)

    assert g.weights[("C", "m")] == pytest.approx(math.log1p(4))
    assert ("C", "other_file_m") not in g.weights
    assert g.stats["containment_edges"] == 1
    # Containment is for clustering only, never a dependency.
    assert ("C", "m") not in g.directed and ("m", "C") not in g.directed


def test_hub_is_removed_into_foundation():
    spokes = [f"s{i:02d}" for i in range(12)]
    entities = [_entity("hub")] + [_entity(s) for s in spokes]
    edges = [_edge(s, "hub") for s in spokes] + [_edge("s00", "s01")]

    g = _build(entities, edges, hub_percentile=90)

    assert g.foundation == {"hub"}
    assert all("hub" not in pair for pair in g.weights)
    # Directed counts keep the hub's edges for ordering (§7).
    assert g.directed[("s05", "hub")].calls == 1
    assert g.stats["hubs"] == 1


def test_no_hub_below_minimum_degree():
    entities = [_entity("hub")] + [_entity(f"s{i}") for i in range(5)]
    edges = [_edge(f"s{i}", "hub") for i in range(5)]
    g = _build(entities, edges, hub_percentile=50)
    assert g.foundation == set()


def test_hub_method_takes_its_whole_class_into_foundation():
    spokes = [f"s{i:02d}" for i in range(12)]
    entities = [
        _entity("C", name="Registry", entity_type="class", raw_facts={}),
        _entity("get", name="get", raw_facts={"class": "Registry"}),
        _entity("set", name="set", raw_facts={"class": "Registry"}),
    ] + [_entity(s) for s in spokes]
    edges = [_edge(s, "get") for s in spokes]

    g = _build(entities, edges, hub_percentile=90)

    assert g.foundation == {"C", "get", "set"}


def test_isolated_node_detected():
    entities = [
        _entity("a"),
        _entity("b"),
        _entity("cpt", name="book", entity_type="schema", raw_facts={"kind": "post_type"}),
    ]
    g = _build(entities, [_edge("a", "b")])
    assert g.isolated == {"cpt"}
    assert g.stats["isolated"] == 1


def _seed_source_job(client, org_id="org-1", job_id="job-1"):
    client.table("extracted_entities").insert(
        [
            {**_entity("a"), "org_id": org_id, "job_id": job_id},
            {**_entity("b"), "org_id": org_id, "job_id": job_id},
            {**_entity("c"), "org_id": org_id, "job_id": job_id},
        ]
    ).execute()
    client.table("entity_edges").insert(
        [
            {**_edge("a", "b"), "id": "edge-1", "org_id": org_id, "job_id": job_id},
            {**_edge("a", "b"), "id": "edge-2", "org_id": org_id, "job_id": job_id},
            {**_edge("a", None, "include"), "id": "edge-3", "org_id": org_id, "job_id": job_id},
            {**_edge("a", "c", "global"), "id": "edge-4", "org_id": org_id, "job_id": job_id},
            {**_edge("b", None), "id": "edge-5", "org_id": org_id, "job_id": job_id},
        ]
    ).execute()
    client.table("knowledge_summaries").insert(
        [
            {**_summary("a", "[1,0]"), "org_id": org_id, "job_id": job_id, "level": "entity"},
            {**_summary(None, "[1,0]"), "org_id": org_id, "job_id": job_id, "level": "module"},
        ]
    ).execute()


def _planning_job(client, org_id="org-1"):
    job = {
        "id": "pj-1",
        "org_id": org_id,
        "project_id": "project-1",
        "source_job_id": "job-1",
        "status": "running",
        "params": {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0},
    }
    client.table("planning_jobs").insert(job).execute()
    return job


def test_run_graph_stage_ignores_include_global_and_null_targets_and_writes_stats():
    client = FakeClient()
    _seed_source_job(client)
    job = _planning_job(client)

    g = run_graph_stage(client, job)

    assert set(g.directed) == {("a", "b")}
    assert g.directed[("a", "b")].calls == 2
    assert g.isolated == {"c"}
    stored = client.table("planning_jobs").rows[0]["graph_stats"]
    assert stored == g.stats
    assert stored["nodes"] == 3 and stored["call_rows"] == 2


def test_run_graph_stage_scopes_every_load_to_org_and_source_job():
    client = FakeClient()
    _seed_source_job(client)
    client.table("extracted_entities").insert(
        [
            {**_entity("foreign"), "org_id": "org-2", "job_id": "job-1"},
            {**_entity("older"), "org_id": "org-1", "job_id": "job-0"},
        ]
    ).execute()
    client.table("entity_edges").insert(
        {**_edge("a", "c"), "id": "edge-x", "org_id": "org-2", "job_id": "job-1"}
    ).execute()
    job = _planning_job(client)

    g = run_graph_stage(client, job)

    assert set(g.nodes) == {"a", "b", "c"}
    assert ("a", "c") not in g.directed


def test_run_graph_stage_pages_past_the_row_cap(monkeypatch):
    monkeypatch.setattr(graph_module, "_PAGE_SIZE", 2)
    client = FakeClient()
    _seed_source_job(client)
    job = _planning_job(client)

    g = run_graph_stage(client, job)

    assert set(g.nodes) == {"a", "b", "c"}
    assert g.directed[("a", "b")].calls == 2


def test_graph_stats_write_is_scoped_by_org():
    client = FakeClient()
    _seed_source_job(client)
    job = _planning_job(client)
    other = {**job, "id": "pj-1", "org_id": "org-2"}
    client.table("planning_jobs").insert({**other, "graph_stats": None}).execute()

    run_graph_stage(client, job)

    rows = {r["org_id"]: r for r in client.table("planning_jobs").rows}
    assert rows["org-2"]["graph_stats"] is None
    assert rows["org-1"]["graph_stats"] is not None
