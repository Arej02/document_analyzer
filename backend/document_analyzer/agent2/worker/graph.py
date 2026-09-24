"""Stage `graph`: build the canonical weighted graph — design_agent2.md §3.

Reads Agent 1's tables for one `source_job_id` (read-only, every query also
filtered by the planning job's `org_id`) and builds, in memory:

- canonical nodes, with duplicate entities across extractors collapsed (§3.2);
- directed per-pair `calls` / `hook_binding` counts between canonical nodes,
  kept for the dependency graph (§7);
- the symmetrized, undirected §3.5 weights used for clustering, plus
  class<->method containment edges (§3.3), with hubs removed (§3.4);
- the foundation (hub) set and the isolated nodes (§3.4).

The only write is `planning_jobs.graph_stats`, filtered by `id` + `org_id`.
Stage `cluster` (§4) consumes the returned `CanonicalGraph`.
"""

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

from supabase import Client

# Edge types that become graph edges (§3.1). `include` (always a null
# target) and `global` are ignored here; their facts stay in `raw_facts`.
GRAPH_EDGE_TYPES = ("calls", "hook_binding")

# §3.4: a hub must also have at least this many distinct neighbours, so the
# top percentile of a small or sparse repo isn't pulled into the foundation.
MIN_HUB_DEGREE = 10

# PostgREST caps a response at 1000 rows by default; page every load.
_PAGE_SIZE = 1000


@dataclass
class EdgeCounts:
    calls: int = 0
    hook_bindings: int = 0


@dataclass
class CanonicalGraph:
    # canonical entity id -> its extracted_entities row
    nodes: dict[str, dict]
    # canonical entity id -> every original entity id it stands for (itself included)
    members: dict[str, list[str]]
    # (from, to) canonical ids -> directed counts; hubs included, self-loops dropped (§7 input)
    directed: dict[tuple[str, str], EdgeCounts]
    # (a, b) with a < b -> §3.5 weight, containment included, hubs excluded (§4 input)
    weights: dict[tuple[str, str], float]
    foundation: set[str] = field(default_factory=set)
    isolated: set[str] = field(default_factory=set)
    # (a, b) with a < b: class <-> method pairs outside the foundation (§3.3); stage
    # `cluster` keeps each class and its methods in one unit
    containment: set[tuple[str, str]] = field(default_factory=set)
    stats: dict = field(default_factory=dict)


def _fetch_all(query_factory) -> list[dict]:
    """Pages through a query ordered by `id`. `query_factory()` must return a fresh,
    fully filtered query each call."""
    rows: list[dict] = []
    start = 0
    while True:
        page = query_factory().order("id").range(start, start + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _load_entities(client: Client, source_job_id: str, org_id: str) -> list[dict]:
    return _fetch_all(
        lambda: client.table("extracted_entities")
        .select("id, entity_type, name, file_path, raw_facts")
        .eq("job_id", source_job_id)
        .eq("org_id", org_id)
    )


def _load_entity_summaries(client: Client, source_job_id: str, org_id: str) -> dict[str, dict]:
    rows = _fetch_all(
        lambda: client.table("knowledge_summaries")
        .select("id, entity_id, grounding_status, embedding")
        .eq("job_id", source_job_id)
        .eq("org_id", org_id)
        .eq("level", "entity")
    )
    return {row["entity_id"]: row for row in rows if row.get("entity_id")}


def _load_edges(client: Client, source_job_id: str, org_id: str) -> list[dict]:
    rows = _fetch_all(
        lambda: client.table("entity_edges")
        .select("id, from_entity_id, to_entity_id, edge_type")
        .eq("job_id", source_job_id)
        .eq("org_id", org_id)
        .in_("edge_type", list(GRAPH_EDGE_TYPES))
    )
    # Null targets are dropped here rather than in the query; an unresolved
    # edge can't connect two nodes whatever its type.
    return [row for row in rows if row["edge_type"] in GRAPH_EDGE_TYPES and row.get("to_entity_id")]


def _parse_embedding(value) -> list[float] | None:
    # pgvector columns come back over PostgREST as the text "[0.1,0.2,...]".
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return [float(x) for x in value]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _canonical_key(entity: dict) -> tuple:
    # §3.2 groups on (file_path, name, entity_type). Methods additionally key on
    # their class, so two classes in one file that each define e.g. __construct
    # don't collapse into one node (design_agent2.md §3.2).
    raw_facts = entity.get("raw_facts") or {}
    return (entity["file_path"], entity["name"], entity["entity_type"], raw_facts.get("class"))


def _canonicalize(entities: list[dict], summaries: dict[str, dict]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Returns (entity id -> canonical id, canonical id -> member ids)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for entity in entities:
        groups[_canonical_key(entity)].append(entity)

    canonical_of: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for group in groups.values():
        # Prefer an entity with a summary, then PhpExtractor's (WordPressExtractor's
        # callback duplicates carry raw_facts.role = hook_callback), then lowest id
        # so the choice is deterministic.
        representative = min(
            group,
            key=lambda e: (
                e["id"] not in summaries,
                (e.get("raw_facts") or {}).get("role") == "hook_callback",
                e["id"],
            ),
        )
        member_ids = sorted(e["id"] for e in group)
        members[representative["id"]] = member_ids
        for member_id in member_ids:
            canonical_of[member_id] = representative["id"]
    return canonical_of, members


def _containment_pairs(nodes: dict[str, dict]) -> set[tuple[str, str]]:
    """§3.3: class <-> each method whose raw_facts names that class, same file."""
    class_by_file_and_name: dict[tuple[str, str], str] = {}
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if node["entity_type"] == "class":
            class_by_file_and_name.setdefault((node["file_path"], node["name"]), node_id)

    pairs = set()
    for node_id, node in nodes.items():
        class_name = (node.get("raw_facts") or {}).get("class")
        if node["entity_type"] != "function" or not class_name:
            continue
        class_id = class_by_file_and_name.get((node["file_path"], class_name))
        if class_id is not None:
            pairs.add(tuple(sorted((class_id, node_id))))
    return pairs


def _hubs(nodes: dict[str, dict], undirected: set[tuple[str, str]], hub_percentile: float) -> set[str]:
    """§3.4: nodes whose distinct-neighbour degree is >= the percentile threshold
    (nearest rank over every node) and >= MIN_HUB_DEGREE."""
    if not nodes:
        return set()
    degree = dict.fromkeys(nodes, 0)
    for a, b in undirected:
        degree[a] += 1
        degree[b] += 1
    ordered = sorted(degree.values())
    rank = max(1, math.ceil(hub_percentile / 100 * len(ordered)))
    threshold = max(ordered[rank - 1], MIN_HUB_DEGREE)
    return {node_id for node_id, d in degree.items() if d >= threshold}


def build_graph(
    entities: list[dict],
    summaries: dict[str, dict],
    edges: list[dict],
    *,
    alpha: float,
    hub_percentile: float,
) -> CanonicalGraph:
    """Pure §3 graph build over already-loaded rows (no DB access)."""
    canonical_of, members = _canonicalize(entities, summaries)
    nodes = {e["id"]: e for e in entities if canonical_of[e["id"]] == e["id"]}

    # §3.1: per-pair counts, one `calls` row per call site, rewired onto canonical ids.
    directed: dict[tuple[str, str], EdgeCounts] = defaultdict(EdgeCounts)
    for edge in edges:
        src = canonical_of.get(edge["from_entity_id"])
        dst = canonical_of.get(edge["to_entity_id"])
        if src is None or dst is None or src == dst:
            continue
        counts = directed[(src, dst)]
        if edge["edge_type"] == "calls":
            counts.calls += 1
        else:
            counts.hook_bindings += 1
    directed = dict(directed)

    undirected_totals: dict[tuple[str, str], int] = defaultdict(int)
    for (src, dst), counts in directed.items():
        undirected_totals[tuple(sorted((src, dst)))] += counts.calls + counts.hook_bindings

    # §3.4 hubs, by degree on the dependency edges only (containment is a
    # clustering aid, not a dependency). A hub inside a class takes the whole
    # class with it, so the foundation never splits a class (§3.3).
    hubs = _hubs(nodes, set(undirected_totals), hub_percentile)
    containment = _containment_pairs(nodes)
    class_groups: dict[str, set[str]] = defaultdict(set)
    for a, b in containment:
        class_id = a if nodes[a]["entity_type"] == "class" else b
        class_groups[class_id].update((a, b))
    foundation = set(hubs)
    for group in class_groups.values():
        if group & hubs:
            foundation |= group

    embeddings: dict[str, list[float]] = {}
    for node_id in nodes:
        summary = summaries.get(node_id)
        if summary and summary.get("grounding_status") == "verified":
            embedding = _parse_embedding(summary.get("embedding"))
            if embedding:
                embeddings[node_id] = embedding

    # §3.5 weight; the similarity term only when both endpoints have a verified summary.
    weights: dict[tuple[str, str], float] = {}
    for (a, b), total in undirected_totals.items():
        if a in foundation or b in foundation:
            continue
        weight = math.log1p(total)
        if alpha and a in embeddings and b in embeddings:
            weight += alpha * max(0.0, _cosine(embeddings[a], embeddings[b]))
        weights[(a, b)] = weight

    # §3.3 containment at W_CONTAIN = the max observed edge weight.
    w_contain = max(weights.values(), default=math.log1p(1))
    kept_containment = {pair for pair in containment if pair[0] not in foundation and pair[1] not in foundation}
    for pair in kept_containment:
        weights[pair] = max(weights.get(pair, 0.0), w_contain)

    connected = {node_id for pair in weights for node_id in pair}
    isolated = {node_id for node_id in nodes if node_id not in foundation and node_id not in connected}

    stats = {
        "entities": len(entities),
        "nodes": len(nodes),
        "canonicalized": len(entities) - len(nodes),
        "call_rows": sum(c.calls for c in directed.values()),
        "hook_binding_rows": sum(c.hook_bindings for c in directed.values()),
        "directed_edges": len(directed),
        "weighted_edges": len(weights),
        "containment_edges": len(kept_containment),
        "hubs": len(hubs),
        "foundation": len(foundation),
        "isolated": len(isolated),
    }
    return CanonicalGraph(
        nodes=nodes,
        members=members,
        directed=directed,
        weights=weights,
        foundation=foundation,
        isolated=isolated,
        containment=kept_containment,
        stats=stats,
    )


def write_graph_stats(client: Client, planning_job_id: str, org_id: str, stats: dict) -> None:
    (
        client.table("planning_jobs")
        .update({"graph_stats": stats})
        .eq("id", planning_job_id)
        .eq("org_id", org_id)
        .execute()
    )


def run_graph_stage(client: Client, planning_job: dict) -> CanonicalGraph:
    """Loads the source job's rows, builds the §3 graph and records `graph_stats`."""
    org_id = planning_job["org_id"]
    source_job_id = planning_job["source_job_id"]
    params = planning_job["params"]

    entities = _load_entities(client, source_job_id, org_id)
    summaries = _load_entity_summaries(client, source_job_id, org_id)
    edges = _load_edges(client, source_job_id, org_id)

    graph = build_graph(
        entities,
        summaries,
        edges,
        alpha=float(params["alpha"]),
        hub_percentile=float(params["hub_percentile"]),
    )
    write_graph_stats(client, planning_job["id"], org_id, graph.stats)
    return graph
