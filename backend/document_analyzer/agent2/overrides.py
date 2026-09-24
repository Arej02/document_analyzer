"""Plan overrides → a new plan version — design_agent2.md §9.2.

`POST /plans/{plan_id}/overrides` records one override against version *n* and
writes version *n+1*. The recomputation is pure Python over what the planning job
already stored: version 1's plan items (the post-merge units, their phases and
test contracts), `clusters`, `cluster_members`, `cluster_reports` and
`cluster_edges`. There's no LLM call, no Leiden and no Agent 1 read.

Every version is rebuilt from version 1 by replaying the whole override chain
(each version's `plan_overrides` row points at the version it produced through
`result_plan_id`), then the new override. Replaying keeps plan item ids
deterministic, so an id a reviewer saw on version *n* means the same item here.

Kinds (§9.2):
- `merge` combines two or more plan items of one phase into one. The item keeps the
  lowest unit id, with every underlying unit in `test_contract.merged_cluster_ids`.
  A merge that would close a new dependency cycle is refused (422).
- `facade_ack` acknowledges one `needs_facade` SCC (`level` + `scc_id`), which takes
  it out of `open_facade_flags`. A note is required.
- `reorder` pins an item to a `unit_order` within its phase, and only if every
  dependency inside the phase still comes first (422 otherwise).
- `split` is out of the MVP (422). It would need entity-level edges and a way to key
  a plan item without a `clusters` row (design_agent2.md §9.2).
"""

from dataclasses import dataclass, field

import networkx as nx

from document_analyzer.agent2.worker.ordering import _combine, _Key, _sorted_members
from document_analyzer.worker.risk_rollup import rollup_risk

_PAGE_SIZE = 1000
_IN_CHUNK = 200


class OverrideError(ValueError):
    """An override that can't be applied: an invalid payload, or one that would break
    the plan's dependency order. The API answers 422 with the message."""


@dataclass
class BasePlan:
    """What every version of one planning job is recomputed from (version 1 + stored
    clusters/edges). Unit ids are version 1's plan item ids (post-merge units, §7)."""

    phase_of: dict[str, str]  # unit id -> phase cluster id
    phase_order: dict[str, int]  # phase cluster id -> phase_order
    keys: dict[str, _Key]  # unit id -> §8 tie key
    contracts: dict[str, dict]  # unit id -> version 1 test_contract
    # Unit-level dependency edges between post-merge units (from depends on to), with
    # whether the edge belongs to a needs_facade SCC.
    unit_edges: dict[tuple[str, str], bool]
    # (level, scc_id) -> the unit or phase cluster ids at the ends of its edges
    facade_sccs: dict[tuple[str, int], set[str]]
    labels: dict[str, str] = field(default_factory=dict)  # unit id -> clusters.label


@dataclass
class PlanState:
    """The override chain applied so far."""

    units_of: dict[str, list[str]]  # plan item id -> its units (sorted)
    pins: list[tuple[str, int]] = field(default_factory=list)  # (item id, unit_order)
    acks: set[tuple[str, int]] = field(default_factory=set)  # acknowledged (level, scc_id)

    def item_of(self) -> dict[str, str]:
        return {unit: item for item, units in self.units_of.items() for unit in units}


def _fetch_all(query_factory, order_column: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = query_factory().order(order_column).range(start, start + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _fetch_in_chunks(query_for_ids, ids: list[str]) -> list[dict]:
    rows: list[dict] = []
    for start in range(0, len(ids), _IN_CHUNK):
        rows.extend(query_for_ids(ids[start : start + _IN_CHUNK]).execute().data)
    return rows


def load_chain(client, org_id: str, plan: dict) -> tuple[dict, list[dict]]:
    """(version 1 of this plan's planning job, the overrides that led from it to
    `plan`, oldest first). Each non-root version has exactly one override whose
    `result_plan_id` is that version."""
    overrides: list[dict] = []
    current = plan
    while current.get("parent_plan_id"):
        rows = (
            client.table("plan_overrides")
            .select("id, plan_id, kind, payload, created_by, created_at")
            .eq("org_id", org_id)
            .eq("result_plan_id", current["id"])
            .limit(1)
            .execute()
        ).data
        if not rows:
            raise RuntimeError(f"plan {current['id']} has a parent but no override that produced it")
        overrides.append(rows[0])
        parent = (
            client.table("migration_plans")
            .select("id, parent_plan_id, version")
            .eq("org_id", org_id)
            .eq("id", current["parent_plan_id"])
            .limit(1)
            .execute()
        ).data
        if not parent:
            raise RuntimeError(f"plan {current['id']} points at a missing parent {current['parent_plan_id']}")
        current = parent[0]
    overrides.reverse()
    return current, overrides


def acknowledged_sccs(overrides: list[dict]) -> set[tuple[str, int]]:
    return {
        (o["payload"]["level"], int(o["payload"]["scc_id"]))
        for o in overrides
        if o["kind"] == "facade_ack"
    }


def load_base(client, org_id: str, root_plan_id: str, planning_job_id: str) -> BasePlan:
    items = _fetch_all(
        lambda: client.table("migration_plan_items")
        .select("cluster_id, phase_cluster_id, phase_order, unit_order, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", root_plan_id),
        order_column="cluster_id",
    )
    unit_ids = sorted(i["cluster_id"] for i in items)
    clusters = {
        row["id"]: row
        for row in _fetch_all(
            lambda: client.table("clusters")
            .select("id, kind, label")
            .eq("org_id", org_id)
            .eq("planning_job_id", planning_job_id),
            order_column="id",
        )
    }
    canonical: dict[str, set[str]] = {}
    for row in _fetch_in_chunks(
        lambda ids: client.table("cluster_members")
        .select("cluster_id, canonical_entity_id")
        .eq("org_id", org_id)
        .in_("cluster_id", ids),
        unit_ids,
    ):
        canonical.setdefault(row["cluster_id"], set()).add(row["canonical_entity_id"])

    job = (
        client.table("planning_jobs")
        .select("graph_stats")
        .eq("org_id", org_id)
        .eq("id", planning_job_id)
        .limit(1)
        .execute()
    ).data
    merged_into = ((job[0].get("graph_stats") if job else None) or {}).get("merged_into") or {}
    merged_from: dict[str, list[str]] = {}
    for original, merged in sorted(merged_into.items()):
        merged_from.setdefault(merged, []).append(original)

    report_ids = sorted({s for u in unit_ids for s in merged_from.get(u, [u])})
    risk = {
        row["cluster_id"]: row["risk_level"]
        for row in _fetch_in_chunks(
            lambda ids: client.table("cluster_reports")
            .select("cluster_id, risk_level")
            .eq("org_id", org_id)
            .in_("cluster_id", ids),
            report_ids,
        )
    }

    keys = {}
    for unit_id in unit_ids:
        # Same tie key as the worker's (§8): a merged_scc unit rolls up its originals' risk.
        sources = merged_from.get(unit_id, [unit_id])
        unit_risk, _ = rollup_risk([(s, risk.get(s, "low")) for s in sources])
        nodes = sorted(canonical.get(unit_id, ()))
        keys[unit_id] = _Key(
            (clusters.get(unit_id) or {}).get("kind") == "foundation", unit_risk, len(nodes), nodes[0] if nodes else unit_id
        )

    unit_edges: dict[tuple[str, str], bool] = {}
    facade_sccs: dict[tuple[str, int], set[str]] = {}
    for edge in _fetch_all(
        lambda: client.table("cluster_edges")
        .select("id, level, from_cluster_id, to_cluster_id, flag, scc_id")
        .eq("org_id", org_id)
        .eq("planning_job_id", planning_job_id),
        order_column="id",
    ):
        ends = (edge["from_cluster_id"], edge["to_cluster_id"])
        if edge["flag"] == "needs_facade" and edge.get("scc_id") is not None:
            facade_sccs.setdefault((edge["level"], int(edge["scc_id"])), set()).update(ends)
        # `merged` edges run between a merged_scc unit's originals, inside one item.
        if edge["level"] == "unit" and edge["flag"] != "merged":
            unit_edges[ends] = unit_edges.get(ends, False) or edge["flag"] == "needs_facade"

    return BasePlan(
        phase_of={i["cluster_id"]: i["phase_cluster_id"] for i in items},
        phase_order={i["phase_cluster_id"]: i["phase_order"] for i in items},
        keys=keys,
        contracts={i["cluster_id"]: i.get("test_contract") or {} for i in items},
        unit_edges=unit_edges,
        facade_sccs=facade_sccs,
        labels={u: clusters[u]["label"] for u in unit_ids if (clusters.get(u) or {}).get("label")},
    )


def initial_state(base: BasePlan) -> PlanState:
    return PlanState(units_of={u: [u] for u in sorted(base.phase_of)})


def _phase_of_item(base: BasePlan, state: PlanState, item_id: str) -> str:
    return base.phase_of[state.units_of[item_id][0]]


def _item_edges(base: BasePlan, state: PlanState) -> dict[tuple[str, str], bool]:
    """Unit edges mapped onto plan items (self-loops dropped), with whether every
    underlying edge belongs to a needs_facade SCC."""
    item_of = state.item_of()
    edges: dict[tuple[str, str], bool] = {}
    for (a, b), facade in base.unit_edges.items():
        ia, ib = item_of.get(a), item_of.get(b)
        if ia is None or ib is None or ia == ib:
            continue
        edges[(ia, ib)] = edges.get((ia, ib), True) and facade
    return edges


def _label(base: BasePlan, state: PlanState, item_id: str) -> str:
    labels = [base.labels[u] for u in state.units_of.get(item_id, [item_id]) if base.labels.get(u)]
    return f"“{' + '.join(labels)}”" if labels else f"unit {item_id}"


def _apply_merge(base: BasePlan, state: PlanState, payload: dict) -> None:
    ids = payload.get("cluster_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(set(ids)) < 2:
        raise OverrideError("merge needs `cluster_ids`: two or more distinct plan item ids")
    missing = sorted(i for i in set(ids) if i not in state.units_of)
    if missing:
        raise OverrideError(f"not a unit of this plan version: {', '.join(missing)}")
    phases = {_phase_of_item(base, state, i) for i in ids}
    if len(phases) > 1:
        raise OverrideError("merge only combines units of the same phase")

    units = sorted(u for i in set(ids) for u in state.units_of[i])
    for i in set(ids):
        del state.units_of[i]
    state.units_of[units[0]] = units
    # Earlier pins placed the separate units; the merged item takes its §8 position.
    state.pins = [(item, pos) for item, pos in state.pins if item not in ids]

    # Contracting units can close a cycle that §7 never flagged (a -> x -> b with a, b
    # merged). Only the needs_facade SCCs a reviewer already sees may stay cyclic.
    graph = nx.DiGraph()
    graph.add_nodes_from(state.units_of)
    edges = _item_edges(base, state)
    graph.add_edges_from(edges)
    for component in nx.strongly_connected_components(graph):
        if len(component) < 2:
            continue
        sub = graph.subgraph(component)
        if not all(edges[e] for e in sub.edges):
            raise OverrideError(
                "merging these units would create a new dependency cycle through "
                + ", ".join(_label(base, state, i) for i in sorted(component))
            )


def _apply_facade_ack(base: BasePlan, state: PlanState, payload: dict) -> None:
    level, scc_id, note = payload.get("level"), payload.get("scc_id"), payload.get("note")
    if level not in ("unit", "phase") or not isinstance(scc_id, int) or isinstance(scc_id, bool):
        raise OverrideError("facade_ack needs `level` (unit or phase) and an integer `scc_id`")
    if not isinstance(note, str) or not note.strip():
        raise OverrideError("facade_ack needs a `note` explaining the facade")
    key = (level, scc_id)
    if key not in base.facade_sccs:
        raise OverrideError(f"no needs_facade cycle with scc_id {scc_id} at {level} level")
    if key in state.acks:
        raise OverrideError(f"the {level}-level cycle {scc_id} is already acknowledged")
    state.acks.add(key)


def _apply_reorder(base: BasePlan, state: PlanState, payload: dict) -> None:
    item_id, position = payload.get("cluster_id"), payload.get("unit_order")
    if not isinstance(item_id, str) or not isinstance(position, int) or isinstance(position, bool):
        raise OverrideError("reorder needs `cluster_id` and an integer `unit_order`")
    if item_id not in state.units_of:
        raise OverrideError(f"not a unit of this plan version: {item_id}")
    phase_id = _phase_of_item(base, state, item_id)
    phase_size = sum(1 for i in state.units_of if _phase_of_item(base, state, i) == phase_id)
    if not 1 <= position <= phase_size:
        raise OverrideError(f"unit_order must be between 1 and {phase_size} in this phase")
    state.pins.append((item_id, position))


_APPLY = {"merge": _apply_merge, "facade_ack": _apply_facade_ack, "reorder": _apply_reorder}


def apply_override(base: BasePlan, state: PlanState, kind: str, payload: dict) -> None:
    """Applies one override to `state` in place, or raises OverrideError."""
    if kind == "split":
        raise OverrideError("split overrides aren't part of the MVP")
    if kind not in _APPLY:
        raise OverrideError(f"unknown override kind: {kind}")
    if not isinstance(payload, dict):
        raise OverrideError("payload must be an object")
    _APPLY[kind](base, state, payload)


def _phase_order_of_items(base: BasePlan, state: PlanState, phase_id: str, edges: dict) -> list[str]:
    """§8 step 2 over this phase's items, then every reorder pin in chain order.
    Raises OverrideError when the pins break a dependency inside the phase."""
    items = sorted(i for i in state.units_of if _phase_of_item(base, state, i) == phase_id)
    keys = {i: _combine([base.keys[u] for u in state.units_of[i]]) for i in items}
    sub = nx.DiGraph()
    sub.add_nodes_from(items)
    sub.add_edges_from((a, b) for a, b in edges if a in keys and b in keys)
    order = _sorted_members(nx.condensation(sub), keys)

    for item_id, position in state.pins:
        if item_id in keys:
            order.remove(item_id)
            order.insert(min(position, len(order) + 1) - 1, item_id)

    position_of = {item: n for n, item in enumerate(order)}
    scc_of = {}
    for n, component in enumerate(nx.strongly_connected_components(sub)):
        scc_of.update(dict.fromkeys(component, n))
    for a, b in sorted(sub.edges):
        # Inside a cycle there's no consistent order to keep.
        if scc_of[a] != scc_of[b] and position_of[b] > position_of[a]:
            raise OverrideError(
                f"{_label(base, state, a)} depends on {_label(base, state, b)}, "
                f"so {_label(base, state, b)} must come first in the phase"
            )
    return order


def _remap_contract(contract: dict, item_id: str, units: list[str], contracts: dict, item_of: dict) -> dict:
    """An item's contract over its units' version 1 contracts, with unit ids mapped to
    plan items. Call sites and dependencies between units of the same item become
    internal and drop out. `public_interface` stays the union: a member only called
    by a merged-in unit is still listed, which is the safe side for Agent 3's tests."""
    parts = [contracts.get(u) or {} for u in units]
    if len(units) == 1 and all(
        item_of.get(e.get("from_unit_id"), e.get("from_unit_id")) == e.get("from_unit_id")
        for e in contract.get("incoming_call_sites") or []
    ) and all(
        item_of.get(d.get("to_unit_id"), d.get("to_unit_id")) == d.get("to_unit_id")
        for d in contract.get("outgoing_dependencies") or []
    ):
        return contract  # nothing it references moved: keep version 1's contract as is

    def mapped(unit_id):
        return item_of.get(unit_id, unit_id)

    incoming = sorted(
        (
            {**site, "from_unit_id": mapped(site["from_unit_id"])}
            for part in parts
            for site in part.get("incoming_call_sites") or []
            if mapped(site["from_unit_id"]) != item_id
        ),
        key=lambda s: (s["from_entity_id"], s["to_entity_id"]),  # the worker's (caller, callee) order
    )
    outgoing: dict[str, dict] = {}
    for part in parts:
        for dep in part.get("outgoing_dependencies") or []:
            target = mapped(dep["to_unit_id"])
            if target == item_id:
                continue
            total = outgoing.setdefault(target, {"to_unit_id": target, "call_count": 0, "hook_binding_count": 0})
            total["call_count"] += dep.get("call_count") or 0
            total["hook_binding_count"] += dep.get("hook_binding_count") or 0

    def union_by_entity(field_name):
        by_id = {}
        for part in parts:
            for entry in part.get(field_name) or []:
                by_id.setdefault(entry["entity_id"], entry)
        return [by_id[k] for k in sorted(by_id)]

    return {
        "public_interface": union_by_entity("public_interface"),
        "incoming_call_sites": incoming,
        "outgoing_dependencies": [outgoing[k] for k in sorted(outgoing)],
        "external_calls": sorted({c for part in parts for c in part.get("external_calls") or []}),
        "hooks_bound": sorted({h for part in parts for h in part.get("hooks_bound") or []}),
        "side_effect_signals": union_by_entity("side_effect_signals"),
        "unverified_member_ids": sorted({m for part in parts for m in part.get("unverified_member_ids") or []}),
        "merged_cluster_ids": list(units) if len(units) > 1 else [],
    }


def build_items(base: BasePlan, state: PlanState) -> tuple[list[dict], int]:
    """(plan item rows for the new version, its open_facade_flags). Raises
    OverrideError when the reorder pins can't be honored."""
    edges = _item_edges(base, state)
    item_of = state.item_of()
    rows = []
    for phase_id in sorted(set(base.phase_of.values()), key=base.phase_order.__getitem__):
        for unit_order, item_id in enumerate(_phase_order_of_items(base, state, phase_id, edges), start=1):
            units = state.units_of[item_id]
            rows.append(
                {
                    "cluster_id": item_id,
                    "phase_cluster_id": phase_id,
                    "phase_order": base.phase_order[phase_id],
                    "unit_order": unit_order,
                    "test_contract": _remap_contract(
                        base.contracts.get(item_id) or {}, item_id, units, base.contracts, item_of
                    ),
                }
            )
    return rows, open_facade_flags(base, state)


def open_facade_flags(base: BasePlan, state: PlanState) -> int:
    """§7 flag count after the chain: a needs_facade SCC stops counting once it's
    acknowledged, or once a merge folds every unit of a unit-level SCC into one item."""
    item_of = state.item_of()
    count = 0
    for (level, scc_id), ends in base.facade_sccs.items():
        if (level, scc_id) in state.acks:
            continue
        if level == "unit" and len({item_of.get(u, u) for u in ends}) < 2:
            continue
        count += 1
    return count


def replay(base: BasePlan, overrides: list[dict]) -> PlanState:
    state = initial_state(base)
    for override in overrides:
        apply_override(base, state, override["kind"], override["payload"])
    return state
