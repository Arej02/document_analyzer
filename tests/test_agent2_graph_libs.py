"""Phase 0 smoke test: the Agent 2 graph libraries work on this Python.

Functional, not just an import check: seeded Leiden must be reproducible
(design_agent2.md §4 determinism rule) and networkx must provide the SCC and
deterministic topological sort used for ordering (§7–§8).
"""

import igraph as ig
import leidenalg
import networkx as nx


def _two_cliques_graph() -> ig.Graph:
    # Two 5-cliques joined by a single weak bridge: an obvious 2-community split.
    edges = []
    weights = []
    for base in (0, 5):
        for i in range(base, base + 5):
            for j in range(i + 1, base + 5):
                edges.append((i, j))
                weights.append(1.0)
    edges.append((4, 5))
    weights.append(0.1)
    g = ig.Graph(n=10, edges=edges)
    g.es["weight"] = weights
    return g


def _run_leiden(g: ig.Graph, seed: int) -> list[int]:
    partition = leidenalg.find_partition(
        g,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=1.0,
        n_iterations=-1,  # run to convergence
        seed=seed,
    )
    return list(partition.membership)


def test_seeded_leiden_is_deterministic():
    first = _run_leiden(_two_cliques_graph(), seed=42)
    second = _run_leiden(_two_cliques_graph(), seed=42)
    assert first == second


def test_leiden_finds_the_obvious_communities():
    membership = _run_leiden(_two_cliques_graph(), seed=42)
    assert len(set(membership[:5])) == 1
    assert len(set(membership[5:])) == 1
    assert membership[0] != membership[5]


def test_networkx_scc_and_lexicographical_topological_sort():
    g = nx.DiGraph()
    # a -> b -> c -> a is a 3-cycle; c -> d, d -> e form a tail.
    g.add_edges_from([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e")])

    sccs = {frozenset(c) for c in nx.strongly_connected_components(g)}
    assert frozenset({"a", "b", "c"}) in sccs
    assert frozenset({"d"}) in sccs
    assert frozenset({"e"}) in sccs

    condensed = nx.condensation(g)
    mapping = condensed.graph["mapping"]
    order = list(
        nx.lexicographical_topological_sort(
            condensed, key=lambda n: min(condensed.nodes[n]["members"])
        )
    )
    assert order == [mapping["a"], mapping["d"], mapping["e"]]
    assert order == list(
        nx.lexicographical_topological_sort(
            condensed, key=lambda n: min(condensed.nodes[n]["members"])
        )
    )
