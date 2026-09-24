"""Tests for stage `cluster` (agent2/worker/cluster.py) — design_agent2.md §4.

Graphs are built with the real stage-`graph` builder (`build_graph`) so the
input has the same shape the planner worker will hand over.
"""

import pytest

from document_analyzer.agent2.worker.cluster import build_clusters, run_cluster_stage
from document_analyzer.agent2.worker.graph import build_graph

from fakes import FakeClient

PARAMS = {"seed": 42, "alpha": 0.0, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}


def _entity(entity_id, file_path="a.php", entity_type="function", raw_facts=None, name=None):
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "name": name or entity_id,
        "file_path": file_path,
        "raw_facts": raw_facts if raw_facts is not None else {"params": []},
    }


def _edge(src, dst):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": "calls"}


def _clique(prefix, n, file_path="a.php"):
    ids = [f"{prefix}{i:02d}" for i in range(n)]
    entities = [_entity(i, file_path=file_path) for i in ids]
    edges = [_edge(a, b) for idx, a in enumerate(ids) for b in ids[idx + 1 :]]
    return entities, edges


def _graph(entities, edges, **params):
    p = {**PARAMS, **params}
    return build_graph(entities, {}, edges, alpha=p["alpha"], hub_percentile=p["hub_percentile"])


def _many_cliques(count=6, size=8):
    """`count` cliques of `size` in a ring joined by single weak edges."""
    entities, edges = [], []
    for c in range(count):
        e, ed = _clique(f"c{c}_", size, file_path=f"f{c}.php")
        entities += e
        edges += ed
        edges.append(_edge(f"c{c}_00", f"c{(c + 1) % count}_01"))
    return entities, edges


def _unit_sets(result):
    return sorted(tuple(u.nodes) for u in result.units)


def _phase_sets(result):
    nodes_of = {u.id: u.nodes for u in result.units}
    return sorted(tuple(sorted(n for uid in p.unit_ids for n in nodes_of[uid])) for p in result.phases)


def test_every_unit_within_cap_unless_foundation_or_recorded_oversized():
    entities, edges = _many_cliques()
    # A dense core over the cap; degree 9 keeps it under the hub floor, so it isn't foundation.
    big_e, big_ed = _clique("big", 10, file_path="big.php")
    g = _graph(entities + big_e, edges + big_ed, hub_percentile=100)
    assert g.foundation == set()
    params = {**PARAMS, "max_unit_size": 5}

    result = build_clusters(g, params)

    for unit in result.units:
        assert unit.kind == "foundation" or len(unit.nodes) <= 5 or unit.id in result.oversized_unit_ids
    for unit_id in result.oversized_unit_ids:
        assert len(next(u for u in result.units if u.id == unit_id).nodes) > 5
    # Every canonical node lands in exactly one unit.
    all_nodes = [n for u in result.units for n in u.nodes]
    assert sorted(all_nodes) == sorted(g.nodes)


def test_oversized_community_is_split_recursively():
    entities, edges = _many_cliques(count=4, size=8)
    g = _graph(entities, edges)

    result = build_clusters(g, {**PARAMS, "max_unit_size": 8, "leiden_resolution": 0.01})

    # At resolution 0.01 the first pass lumps the ring together; the doubling split fixes it.
    assert all(len(u.nodes) <= 8 for u in result.units)
    assert result.oversized_unit_ids == []


def test_every_unit_has_exactly_one_phase():
    entities, edges = _many_cliques()
    spokes = [_edge(f"c{c}_{i:02d}", "hub") for c in range(6) for i in range(2, 4)]
    g = _graph(entities + [_entity("hub")], edges + spokes, hub_percentile=90)

    result = build_clusters(g, PARAMS)

    phase_of = {}
    for phase in result.phases:
        for unit_id in phase.unit_ids:
            assert unit_id not in phase_of
            phase_of[unit_id] = phase.id
    assert {u.id: u.phase_id for u in result.units} == phase_of


def test_foundation_unit_is_its_own_phase():
    entities, edges = _many_cliques()
    spokes = [_edge(f"c{c}_{i:02d}", "hub") for c in range(6) for i in range(2, 4)]
    g = _graph(entities + [_entity("hub")], edges + spokes, hub_percentile=90)

    result = build_clusters(g, PARAMS)

    foundation = [u for u in result.units if u.kind == "foundation"]
    assert len(foundation) == 1 and foundation[0].nodes == ["hub"]
    phase = next(p for p in result.phases if p.id == foundation[0].phase_id)
    assert phase.kind == "foundation" and phase.unit_ids == [foundation[0].id]


def test_class_is_never_split_even_when_over_the_cap():
    methods = [f"m{i:02d}" for i in range(12)]
    entities = [_entity("K", entity_type="class", raw_facts={}, name="Kitchen")]
    entities += [_entity(m, raw_facts={"class": "Kitchen", "params": []}) for m in methods]
    other_e, other_ed = _clique("x", 5, file_path="b.php")
    # Methods only call outside the class, pulling them apart if the class weren't atomic.
    edges = other_ed + [_edge(m, f"x{i % 5:02d}") for i, m in enumerate(methods)]
    g = _graph(entities + other_e, edges, hub_percentile=100)

    result = build_clusters(g, {**PARAMS, "max_unit_size": 5})

    holders = [u for u in result.units if "K" in u.nodes]
    assert len(holders) == 1
    assert set(methods) <= set(holders[0].nodes)
    assert holders[0].id in result.oversized_unit_ids


def test_isolated_node_joins_the_unit_holding_its_file():
    entities, edges = _clique("a", 4, file_path="plugin.php")
    entities.append(_entity("cpt", file_path="plugin.php", entity_type="schema", raw_facts={"kind": "post_type"}))
    g = _graph(entities, edges)

    result = build_clusters(g, PARAMS)

    holder = next(u for u in result.units if "cpt" in u.nodes)
    assert holder.kind == "community" and "a00" in holder.nodes


def test_isolated_nodes_without_a_unit_get_an_isolated_file_unit():
    entities, edges = _clique("a", 4, file_path="plugin.php")
    entities += [_entity(f"meta{i}", file_path="schema.php", entity_type="schema", raw_facts={}) for i in range(3)]
    g = _graph(entities, edges)

    result = build_clusters(g, {**PARAMS, "max_unit_size": 2})

    isolated_units = [u for u in result.units if u.kind == "isolated_file"]
    assert [u.nodes for u in isolated_units] == [["meta0", "meta1"], ["meta2"]]


def test_same_seed_and_input_give_identical_output():
    entities, edges = _many_cliques()
    g1 = _graph(entities, edges)
    g2 = _graph(list(reversed(entities)), list(reversed(edges)))

    first = build_clusters(g1, {**PARAMS, "max_unit_size": 10})
    second = build_clusters(g2, {**PARAMS, "max_unit_size": 10})

    assert _unit_sets(first) == _unit_sets(second)
    assert _phase_sets(first) == _phase_sets(second)


def _cluster_client():
    client = FakeClient()
    client.cascades["clusters"] = [("clusters", "parent_cluster_id"), ("cluster_members", "cluster_id")]
    return client


def _planning_job(org_id="org-1"):
    return {"id": "pj-1", "org_id": org_id, "source_job_id": "job-1", "params": PARAMS}


def test_persist_writes_every_original_entity_with_its_canonical_id():
    entities, edges = _clique("a", 3)
    entities.append(_entity("dup", name="a00", raw_facts={"role": "hook_callback"}))
    g = _graph(entities, edges)
    client = _cluster_client()

    result = run_cluster_stage(client, _planning_job(), g)

    members = client.table("cluster_members").rows
    assert sorted(m["entity_id"] for m in members) == sorted(e["id"] for e in entities)
    dup = next(m for m in members if m["entity_id"] == "dup")
    assert dup["canonical_entity_id"] == "a00"
    assert all(m["org_id"] == "org-1" for m in members)

    clusters = client.table("clusters").rows
    units = [c for c in clusters if c["level"] == "unit"]
    phases = {c["id"] for c in clusters if c["level"] == "phase"}
    assert len(units) == len(result.units)
    assert all(u["parent_cluster_id"] in phases for u in units)
    assert all(c["org_id"] == "org-1" and c["planning_job_id"] == "pj-1" for c in clusters)
    assert units[0]["size"] == 3  # canonical count, not member count


def test_rerun_leaves_no_duplicate_rows():
    entities, edges = _many_cliques()
    g = _graph(entities, edges)
    client = _cluster_client()

    run_cluster_stage(client, _planning_job(), g)
    first = (len(client.table("clusters").rows), len(client.table("cluster_members").rows))
    run_cluster_stage(client, _planning_job(), g)
    second = (len(client.table("clusters").rows), len(client.table("cluster_members").rows))

    assert first == second
    keys = [(m["cluster_id"], m["entity_id"]) for m in client.table("cluster_members").rows]
    assert len(keys) == len(set(keys))


def test_rerun_delete_is_scoped_by_org():
    entities, edges = _clique("a", 3)
    g = _graph(entities, edges)
    client = _cluster_client()
    client.table("clusters").insert(
        {"id": "other", "org_id": "org-2", "planning_job_id": "pj-1", "level": "phase", "parent_cluster_id": None}
    ).execute()

    run_cluster_stage(client, _planning_job(), g)

    assert any(c["id"] == "other" for c in client.table("clusters").rows)


def test_graph_stats_gets_unit_phase_counts_and_oversized_units():
    # A 6-method class over a cap of 3 can't be split, so it's recorded as oversized.
    entities = [_entity("K", entity_type="class", raw_facts={}, name="Kitchen")]
    entities += [_entity(f"m{i}", raw_facts={"class": "Kitchen"}) for i in range(6)]
    entities += [_entity("x"), _entity("y")]
    edges = [_edge("m0", "x"), _edge("x", "y")]
    g = _graph(entities, edges, hub_percentile=100)
    client = _cluster_client()
    client.table("planning_jobs").insert({"id": "pj-1", "org_id": "org-1", "graph_stats": g.stats}).execute()

    result = run_cluster_stage(client, {**_planning_job(), "params": {**PARAMS, "max_unit_size": 3}}, g)

    stats = client.table("planning_jobs").rows[0]["graph_stats"]
    assert stats["nodes"] == 9  # stage graph's stats are kept
    assert stats["units"] == len(result.units) and stats["phases"] == len(result.phases)
    assert len(result.oversized_unit_ids) == 1
    assert [o["cluster_id"] for o in stats["oversized_units"]] == result.oversized_unit_ids
    assert stats["oversized_units"][0]["size"] >= 7


@pytest.mark.parametrize("seed", [1, 7])
def test_empty_graph_produces_no_clusters(seed):
    g = _graph([], [])
    result = build_clusters(g, {**PARAMS, "seed": seed})
    assert result.units == [] and result.phases == []


def _class_with_methods(class_id, file_path, n_methods=2):
    """A class plus its methods: one unit held together by containment only, with no
    edge to anything outside it (the shape of a typical Laravel controller)."""
    entities = [_entity(class_id, file_path=file_path, entity_type="class", raw_facts={}, name=class_id)]
    entities += [
        _entity(f"{class_id}_m{i}", file_path=file_path, raw_facts={"class": class_id}) for i in range(n_methods)
    ]
    return entities


def test_units_with_no_unit_edges_are_pooled_into_one_phase_per_directory():
    entities = (
        _class_with_methods("DoctorController", "app/Http/Controllers/DoctorController.php")
        + _class_with_methods("TeamController", "app/Http/Controllers/TeamController.php")
        + _class_with_methods("Doctor", "app/Models/Doctor.php")
        + _class_with_methods("Team", "app/Models/Team.php")
        + _class_with_methods("Slider", "app/Models/Slider.php")
    )
    g = _graph(entities, [], hub_percentile=100)

    result = build_clusters(g, PARAMS)

    assert len(result.units) == 5
    assert result.directory_phases == 2
    assert _phase_sets(result) == sorted(
        [
            tuple(sorted(n["id"] for n in entities if n["file_path"].startswith("app/Http/Controllers/"))),
            tuple(sorted(n["id"] for n in entities if n["file_path"].startswith("app/Models/"))),
        ]
    )


def test_linked_units_keep_their_leiden_phase_and_only_edgeless_units_are_pooled():
    entities, edges = _many_cliques()
    loose = _class_with_methods("Lonely", "lib/Lonely.php") + _class_with_methods("Alone", "lib/Alone.php")
    g = _graph(entities + loose, edges, hub_percentile=100)

    linked_only = build_clusters(_graph(entities, edges, hub_percentile=100), PARAMS)
    result = build_clusters(g, PARAMS)

    assert result.directory_phases == 1
    loose_ids = tuple(sorted(n["id"] for n in loose))
    assert loose_ids in _phase_sets(result)
    assert [p for p in _phase_sets(result) if p != loose_ids] == _phase_sets(linked_only)


def test_graph_stats_records_directory_phase_count():
    entities = _class_with_methods("A", "x/A.php") + _class_with_methods("B", "y/B.php")
    g = _graph(entities, [], hub_percentile=100)
    client = _cluster_client()
    client.table("planning_jobs").insert({"id": "pj-1", "org_id": "org-1", "graph_stats": g.stats}).execute()

    run_cluster_stage(client, {**_planning_job(), "params": PARAMS}, g)

    stats = client.table("planning_jobs").rows[0]["graph_stats"]
    assert stats["phases"] == 2 and stats["directory_phases"] == 2
