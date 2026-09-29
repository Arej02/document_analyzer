"""`unit_slug` derivation — design_agent3.md §8: `{phase_order:02d}_{unit_order:02d}_{slugified label}`.

A plan item's own `clusters` row has no `label` when it's a `merged_scc` unit
(design_agent2.md §7, the originals were contracted into it) or an override
merge (§9.2, the synthetic row is never given its own label) — the same gap
`shared/unit_sources.py` exists for on the report side. This mirrors
`agent2/router.py::_unit_fields`'s label rule exactly (own label; a
`merged_scc` unit's own label or else its originals' sorted-and-joined
labels; an override merge always joins), so the same plan item gets the same
slug whichever agent looks at it. Falls back to a short id-derived slug when
there is no label at all (for example grounding failed before a label was
ever produced), so `derive_unit_slug` never raises.

Requires `state.clusters` (cluster id -> {kind, label}, loaded by
`prepare.py` for the item's own cluster id and every `report_source_cluster_ids`
entry) and the item's `report_source_cluster_ids` (already attached by
`prepare.py`, `shared/unit_sources.py`).
"""

import re

_SLUG_MAX_CHARS = 40


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug[:_SLUG_MAX_CHARS].strip("_")


def _fallback_slug(cluster_id: str) -> str:
    return "u" + cluster_id.replace("-", "")[:8]


def _unit_label(item: dict, state) -> str | None:
    """`agent2/router.py::_unit_fields`'s label rule, read from `state.clusters`
    instead of a router-local dict."""
    own_cluster = state.clusters.get(item["cluster_id"]) or {}
    sources = item["report_source_cluster_ids"]
    if sources == [item["cluster_id"]]:
        return own_cluster.get("label")

    labels = sorted(
        state.clusters[s]["label"] for s in sources if state.clusters.get(s, {}).get("label")
    )
    if own_cluster.get("kind") == "merged_scc":
        return own_cluster.get("label") or (" + ".join(labels) if labels else None)
    return " + ".join(labels) if labels else own_cluster.get("label")  # override merge: always joins


def derive_unit_slug(item: dict, state) -> str:
    """design_agent3.md §8. Stable within one plan version: `phase_order`/`unit_order`
    come from the plan item, and the label only changes if the plan is replanned."""
    label = _unit_label(item, state)
    slug = _slugify(label) if label else ""
    if not slug:
        slug = _fallback_slug(item["cluster_id"])
    return f"{item['phase_order']:02d}_{item['unit_order']:02d}_{slug}"
