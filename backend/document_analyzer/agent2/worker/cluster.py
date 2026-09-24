"""Stage `cluster`: Leiden, bottom-up, two levels — design_agent2.md §4.

Consumes the in-memory `CanonicalGraph` from stage `graph` (§3) and produces:

1. **Units.** Seeded Leiden (`RBConfigurationVertexPartition`, run to
   convergence) over the weighted graph. A class and its methods (§3.3
   containment) are contracted into one atom first, so a class is never split.
   A community above `max_unit_size` canonical entities is re-split on its
   induced subgraph with the resolution doubled, recursively; one that still
   won't split is kept and recorded in `graph_stats.oversized_units`. Isolated
   nodes are then attached by `file_path`, and the hubs become the foundation.
2. **Phases.** Seeded Leiden over the unit graph (edge weight = sum of member
   weights). The foundation unit is always its own phase. Units with no edge to
   any other unit would each come back as a one-unit phase (one approval
   each), so they are pooled into one phase per directory instead.

Persists `clusters` and `cluster_members` (every original entity id plus its
`canonical_entity_id`) on the service-role client, deleting this planning
job's earlier rows first so a re-run never duplicates (the FKs cascade to
`cluster_members`, `cluster_reports` and `cluster_edges`).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import igraph as ig
import leidenalg
from supabase import Client

from document_analyzer.agent2.worker.graph import CanonicalGraph
from document_analyzer.supabase_client import insert_many_scoped

# Resolution doublings tried on an oversized community before it is recorded
# as oversized (§4 step 1: "a clique-like core").
MAX_SPLIT_DEPTH = 10


@dataclass
class Unit:
    id: str
    kind: str  # community | foundation | isolated_file
    nodes: list[str]  # canonical entity ids, sorted
    phase_id: str | None = None


@dataclass
class Phase:
    id: str
    kind: str  # community | foundation
    unit_ids: list[str] = field(default_factory=list)


@dataclass
class ClusterResult:
    units: list[Unit]
    phases: list[Phase]
    oversized_unit_ids: list[str]
    # Phases built by the directory fallback rather than Leiden (graph_stats).
    directory_phases: int = 0


def _leiden(n: int, edges: list[tuple[int, int]], weights: list[float], resolution: float, seed: int) -> list[int]:
    graph = ig.Graph(n=n, edges=edges)
    graph.es["weight"] = weights
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        n_iterations=-1,  # to convergence
        seed=seed,
    )
    return list(partition.membership)


def _group(keys: list, membership: list[int]) -> list[list]:
    """Groups `keys` by community, each group sorted and the groups sorted, so the
    output doesn't depend on Leiden's community numbering."""
    groups: dict[int, list] = defaultdict(list)
    for key, community in zip(keys, membership):
        groups[community].append(key)
    return sorted(sorted(g) for g in groups.values())


def _partition(keys: list, weights: dict[tuple, float], resolution: float, seed: int) -> list[list]:
    """Leiden over the subgraph induced by `keys` (sorted, for a stable vertex order)."""
    index = {key: i for i, key in enumerate(keys)}
    edges, edge_weights = [], []
    for (a, b), w in sorted(weights.items()):
        if a in index and b in index:
            edges.append((index[a], index[b]))
            edge_weights.append(w)
    return _group(keys, _leiden(len(keys), edges, edge_weights, resolution, seed))


def _atoms(graph: CanonicalGraph, nodes: set[str]) -> dict[str, list[str]]:
    """Atom key (lowest member id) -> canonical nodes. Containment-linked nodes (a
    class and its methods) share one atom; every other node is its own."""
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in graph.containment:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    atoms: dict[str, list[str]] = defaultdict(list)
    for n in sorted(nodes):
        atoms[find(n)].append(n)
    return dict(atoms)


def _community_units(graph: CanonicalGraph, params: dict) -> tuple[list[list[str]], list[list[str]]]:
    """§4 step 1 over the connected nodes. Returns (units, oversized units), each a
    sorted list of canonical ids."""
    connected = {n for pair in graph.weights for n in pair} - graph.foundation
    atoms = _atoms(graph, connected)
    atom_of = {n: key for key, members in atoms.items() for n in members}
    atom_size = {key: len(members) for key, members in atoms.items()}

    atom_weights: dict[tuple[str, str], float] = defaultdict(float)
    for (a, b), w in graph.weights.items():
        if a in atom_of and b in atom_of and atom_of[a] != atom_of[b]:
            atom_weights[tuple(sorted((atom_of[a], atom_of[b])))] += w

    cap = int(params["max_unit_size"])
    seed = int(params["seed"])
    fits: list[list[str]] = []
    oversized: list[list[str]] = []

    def split(atom_keys: list[str], resolution: float, depth: int) -> None:
        for community in _partition(atom_keys, atom_weights, resolution, seed):
            size = sum(atom_size[k] for k in community)
            if size <= cap:
                fits.append(community)
            elif len(community) == 1 or depth >= MAX_SPLIT_DEPTH:
                oversized.append(community)
            else:
                split(community, resolution * 2, depth + 1)

    if atoms:
        split(sorted(atoms), float(params["leiden_resolution"]), 0)

    def expand(atom_keys):
        return sorted(n for k in atom_keys for n in atoms[k])

    return [expand(c) for c in fits], [expand(c) for c in oversized]


def _attach_isolated(
    graph: CanonicalGraph, units: list[list[str]], cap: int
) -> tuple[list[list[str]], list[list[str]]]:
    """§3.4: each isolated node joins the unit holding the most entities from its
    `file_path` while that unit stays within the cap; the rest form one
    `isolated_file` unit per file (chunked at the cap). Returns (units, isolated_file units)."""
    units = [list(u) for u in units]
    by_file: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for i, unit in enumerate(units):
        for n in unit:
            by_file[graph.nodes[n]["file_path"]][i] += 1

    leftovers: dict[str, list[str]] = defaultdict(list)
    for n in sorted(graph.isolated):
        file_path = graph.nodes[n]["file_path"]
        candidates = sorted(by_file.get(file_path, {}).items(), key=lambda item: (-item[1], item[0]))
        target = next((i for i, _ in candidates if len(units[i]) < cap), None)
        if target is None:
            leftovers[file_path].append(n)
        else:
            units[target].append(n)

    isolated_units = []
    for file_path in sorted(leftovers):
        nodes = leftovers[file_path]
        isolated_units.extend(nodes[i : i + cap] for i in range(0, len(nodes), cap))
    return [sorted(u) for u in units], isolated_units


def _unit_directory(graph: CanonicalGraph, unit: Unit) -> str:
    """The directory holding most of the unit's members (ties: smallest path)."""
    counts: dict[str, int] = defaultdict(int)
    for n in unit.nodes:
        counts[PurePosixPath(graph.nodes[n]["file_path"]).parent.as_posix()] += 1
    return min(counts, key=lambda d: (-counts[d], d))


def _phases(graph: CanonicalGraph, units: list[Unit], params: dict) -> tuple[list[list[str]], int]:
    """§4 step 2: Leiden over the non-foundation unit graph, with units that have no
    unit edge at all pooled by directory. Returns (unit-id groups, directory groups)."""
    candidates = [u for u in units if u.kind != "foundation"]
    if not candidates:
        return [], 0
    # Stable, content-derived vertex keys (unit ids are random uuids).
    key_of = {u.id: u.nodes[0] for u in candidates}
    unit_by_key = {u.nodes[0]: u.id for u in candidates}
    unit_of = {n: key_of[u.id] for u in candidates for n in u.nodes}

    unit_weights: dict[tuple[str, str], float] = defaultdict(float)
    for (a, b), w in graph.weights.items():
        ua, ub = unit_of.get(a), unit_of.get(b)
        if ua and ub and ua != ub:
            unit_weights[tuple(sorted((ua, ub)))] += w

    # Leiden can only return an edgeless unit as its own community, so those go
    # to the directory fallback instead of becoming one-unit phases.
    linked = {k for pair in unit_weights for k in pair}
    groups = _partition(sorted(linked), unit_weights, float(params["leiden_resolution"]), int(params["seed"]))

    by_directory: dict[str, list[str]] = defaultdict(list)
    for u in candidates:
        if key_of[u.id] not in linked:
            by_directory[_unit_directory(graph, u)].append(key_of[u.id])
    directory_groups = [sorted(keys) for _, keys in sorted(by_directory.items())]

    return [[unit_by_key[k] for k in group] for group in groups + directory_groups], len(directory_groups)


def build_clusters(graph: CanonicalGraph, params: dict) -> ClusterResult:
    """Pure §4 clustering (no DB access). Same graph + params -> same unit/phase member sets."""
    cap = int(params["max_unit_size"])
    communities, oversized = _community_units(graph, params)
    # Attachment never grows a unit past the cap, so "oversized" is exactly "over the cap" below.
    communities, isolated_units = _attach_isolated(graph, communities + oversized, cap)

    units: list[Unit] = []
    oversized_ids: list[str] = []
    for nodes in communities:
        unit = Unit(id=str(uuid.uuid4()), kind="community", nodes=nodes)
        if len(nodes) > cap:
            oversized_ids.append(unit.id)
        units.append(unit)
    units.extend(Unit(id=str(uuid.uuid4()), kind="isolated_file", nodes=nodes) for nodes in isolated_units)

    phases: list[Phase] = []
    if graph.foundation:
        foundation = Unit(id=str(uuid.uuid4()), kind="foundation", nodes=sorted(graph.foundation))
        units.insert(0, foundation)
        phases.append(Phase(id=str(uuid.uuid4()), kind="foundation", unit_ids=[foundation.id]))
    groups, directory_phases = _phases(graph, units, params)
    phases.extend(Phase(id=str(uuid.uuid4()), kind="community", unit_ids=group) for group in groups)

    unit_by_id = {u.id: u for u in units}
    for phase in phases:
        for unit_id in phase.unit_ids:
            unit_by_id[unit_id].phase_id = phase.id
    return ClusterResult(
        units=units, phases=phases, oversized_unit_ids=oversized_ids, directory_phases=directory_phases
    )


def persist_clusters(client: Client, planning_job: dict, graph: CanonicalGraph, result: ClusterResult) -> None:
    planning_job_id = planning_job["id"]
    org_id = planning_job["org_id"]

    # Idempotent re-run: the FKs cascade to units, members, reports and edges.
    client.table("clusters").delete().eq("planning_job_id", planning_job_id).eq("org_id", org_id).execute()

    unit_by_id = {u.id: u for u in result.units}
    phase_rows = [
        {
            "id": phase.id,
            "planning_job_id": planning_job_id,
            "level": "phase",
            "parent_cluster_id": None,
            "kind": phase.kind,
            "size": sum(len(unit_by_id[uid].nodes) for uid in phase.unit_ids),
        }
        for phase in result.phases
    ]
    unit_rows = [
        {
            "id": unit.id,
            "planning_job_id": planning_job_id,
            "level": "unit",
            "parent_cluster_id": unit.phase_id,
            "kind": unit.kind,
            "size": len(unit.nodes),
        }
        for unit in result.units
    ]
    member_rows = [
        {"cluster_id": unit.id, "entity_id": entity_id, "canonical_entity_id": canonical_id}
        for unit in result.units
        for canonical_id in unit.nodes
        for entity_id in graph.members[canonical_id]
    ]
    # Phases before units (parent FK), units before members.
    for table, rows in (("clusters", phase_rows), ("clusters", unit_rows), ("cluster_members", member_rows)):
        if rows:
            insert_many_scoped(client, table, org_id, rows)


def run_cluster_stage(client: Client, planning_job: dict, graph: CanonicalGraph) -> ClusterResult:
    """Clusters the §3 graph, persists it and adds unit/phase counts and
    `oversized_units` to `graph_stats`."""
    result = build_clusters(graph, planning_job["params"])
    persist_clusters(client, planning_job, graph, result)

    unit_by_id = {u.id: u for u in result.units}
    stats = {
        **graph.stats,
        "units": len(result.units),
        "phases": len(result.phases),
        "directory_phases": result.directory_phases,
        "oversized_units": [{"cluster_id": uid, "size": len(unit_by_id[uid].nodes)} for uid in result.oversized_unit_ids],
    }
    (
        client.table("planning_jobs")
        .update({"graph_stats": stats})
        .eq("id", planning_job["id"])
        .eq("org_id", planning_job["org_id"])
        .execute()
    )
    return result
