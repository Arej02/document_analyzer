"""Agent 2 (Plan) API — design_agent2.md §11.

POST /api/agent2/projects/{project_id}/plans creates a planning_jobs row
(status=queued) and enqueues it on agent2_planning_jobs. Nothing here runs the
planning pipeline inline — see CLAUDE.md "Async by default".
GET /api/agent2/planning-jobs/{planning_job_id} is the matching status poll.
GET /api/agent2/projects/{project_id}/plans lists every plan version, running the
§9.1 stale check first. GET /api/agent2/plans/{plan_id} is one plan's overview, and
GET /api/agent2/plans/{plan_id}/units/{cluster_id} one unit's detail, and
GET /api/agent2/plans/{plan_id}/graph the phase or unit graph for visualization.
POST /api/agent2/plans/{plan_id}/submit moves a draft into review, and
POST /api/agent2/plans/{plan_id}/phases/{phase_cluster_id}/approval records a phase
decision and applies the §9.1 auto-transitions, and
POST /api/agent2/plans/{plan_id}/overrides records a reviewer override and writes the
next plan version (§9.2, recomputed in overrides.py).

The project/source-job lookups go through the caller's RLS-scoped client, same
as Agent 1's endpoints. The planning_jobs insert itself goes through the
service-role client: planning_jobs has no authenticated insert policy
(design_agent2.md §2 "writes are service-role only"), and org_id is stamped
from the server-resolved AuthContext, never from the request.
"""

import os
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from postgrest.exceptions import APIError
from pydantic import BaseModel

from ..auth import AuthContext, get_auth_context
from ..queue import enqueue_planning_job
from ..supabase_client import get_service_role_client, insert_many_scoped, insert_scoped
from ..shared.cost_access import get_cost_operator
from ..shared.cost_report import build_job_cost_report
from ..shared.plan_staleness import STALEABLE_STATUSES as _STALEABLE_STATUSES
from ..shared.plan_staleness import is_stale as _is_stale
from ..shared.plan_staleness import mark_stale_plans
from ..shared.unit_sources import unit_sources as _unit_sources
from ..worker.risk_rollup import rollup_risk
from .overrides import (
    OverrideError,
    acknowledged_sccs,
    apply_override,
    build_items,
    load_base,
    load_chain,
    replay,
)

router = APIRouter(prefix="/api/agent2", tags=["agent2"])

_ACTIVE_STATUSES = ("queued", "running")


# PostgREST caps a response at 1000 rows; plan reads page past it.
_PAGE_SIZE = 1000
# Ids per `in_` filter, so the request URL stays well under common limits.
_IN_CHUNK = 200

# When remapped edges collapse onto one pair, the strongest flag wins.
_FLAG_PRIORITY = {"none": 0, "merged": 1, "needs_facade": 2}

# Postgres unique_violation — planning_jobs_one_active_per_project firing when two
# requests race past the active-job check below.
_UNIQUE_VIOLATION = "23505"

# Attempts at the next plan version number when concurrent overrides collide on it.
_VERSION_RETRIES = 3


class CreatePlanRequest(BaseModel):
    source_job_id: str | None = None


class PhaseApprovalRequest(BaseModel):
    # No reviewer field: the reviewer is always the caller (auth.user_id), so any
    # `reviewer_id` in the body is ignored.
    status: Literal["approved", "rejected"]
    note: str | None = None


class PlanOverrideRequest(BaseModel):
    # The payload's shape depends on `kind` (design_agent2.md §11); overrides.py
    # validates it and answers 422 with a readable message.
    kind: Literal["merge", "split", "facade_ack", "reorder"]
    payload: dict


def planning_params_from_env() -> dict:
    """design_agent2.md §12 defaults, frozen into planning_jobs.params at creation so
    later env changes never alter an existing job's results."""
    return {
        "seed": int(os.environ.get("AGENT2_SEED", 42)),
        "alpha": float(os.environ.get("AGENT2_EDGE_ALPHA", 0.5)),
        "max_unit_size": int(os.environ.get("AGENT2_MAX_UNIT_SIZE", 25)),
        "hub_percentile": float(os.environ.get("AGENT2_HUB_PERCENTILE", 99)),
        "leiden_resolution": float(os.environ.get("AGENT2_LEIDEN_RESOLUTION", 1.0)),
    }


@router.post("/projects/{project_id}/plans", status_code=202)
async def create_plan(
    project_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    body: Annotated[CreatePlanRequest | None, Body()] = None,
):
    project = _get_project_row(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    source_job_id = body.source_job_id if body and body.source_job_id else project.get("latest_job_id")
    if not source_job_id:
        raise HTTPException(status_code=409, detail="project has no ingestion run yet; finish an ingestion run first")

    source_job = _get_source_job(auth.client, auth.org_id, project_id, source_job_id)
    if source_job is None:
        raise HTTPException(status_code=404, detail="source job not found")
    if source_job["status"] != "done":
        raise HTTPException(
            status_code=409, detail=f"source ingestion job is not done yet (status={source_job['status']})"
        )

    if _has_active_planning_job(auth.client, auth.org_id, project_id):
        raise HTTPException(status_code=409, detail="a planning job for this project is already queued or running")

    row = {
        "project_id": project_id,
        "source_job_id": source_job_id,
        "status": "queued",
        "params": planning_params_from_env(),
    }
    try:
        result = insert_scoped(get_service_role_client(), "planning_jobs", auth.org_id, row)
    except APIError as exc:
        if exc.code == _UNIQUE_VIOLATION:
            raise HTTPException(
                status_code=409, detail="a planning job for this project is already queued or running"
            ) from exc
        raise
    planning_job_id = result.data[0]["id"]

    await enqueue_planning_job(planning_job_id=planning_job_id, org_id=auth.org_id)

    return {"planning_job_id": planning_job_id, "status": "queued"}


@router.get("/projects/{project_id}/plans")
async def list_plans(project_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    project = _get_project_row(auth.client, auth.org_id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    latest_job_id = project.get("latest_job_id")
    mark_stale_plans(get_service_role_client(), auth.org_id, project_id, latest_job_id)

    rows = (
        auth.client.table("migration_plans")
        .select("id, version, status, source_job_id, created_at")
        .eq("org_id", auth.org_id)
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data
    return {
        "plans": [
            {
                "id": row["id"],
                "version": row["version"],
                "status": row["status"],
                "source_job_id": row["source_job_id"],
                "is_stale": _is_stale(row["source_job_id"], latest_job_id),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return build_plan_overview(auth.client, auth.org_id, plan)


def load_plan(client, org_id: str, plan_id: str) -> dict | None:
    """One plan row after the §9.1 stale check, or None if missing/other-org. Adds
    `is_stale` (source_job_id ≠ the project's latest_job_id) next to the columns."""
    result = (
        client.table("migration_plans")
        .select(
            "id, project_id, planning_job_id, source_job_id, version, parent_plan_id, status, open_facade_flags, "
            "approved_by, approved_at, created_at"
        )
        .eq("org_id", org_id)
        .eq("id", plan_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    plan = dict(result.data[0])

    project = _get_project_row(client, org_id, plan["project_id"])
    latest_job_id = project.get("latest_job_id") if project else None
    mark_stale_plans(get_service_role_client(), org_id, plan["project_id"], latest_job_id)
    plan["is_stale"] = _is_stale(plan["source_job_id"], latest_job_id)
    if plan["is_stale"] and plan["status"] in _STALEABLE_STATUSES:
        plan["status"] = "stale"
    return plan


def build_plan_overview(client, org_id: str, plan: dict) -> dict:
    """design_agent2.md §11 `GET /plans/{plan_id}`: phases in `phase_order`, each with
    its report fields, approval and units in `unit_order`.

    A unit without a report of its own (a `merged_scc` unit from §7, or an override
    merge from §9.2) takes its label, risk and grounding from its constituent units'
    reports, the same stand-in the worker's ordering uses.
    """
    items = _fetch_all(
        lambda: client.table("migration_plan_items")
        .select("cluster_id, phase_cluster_id, phase_order, unit_order, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", plan["id"]),
        order_column="cluster_id",
    )
    clusters = {
        row["id"]: row
        for row in _fetch_all(
            lambda: client.table("clusters")
            .select("id, level, kind, size, label")
            .eq("org_id", org_id)
            .eq("planning_job_id", plan["planning_job_id"])
        )
    }
    merged_from = _merged_from(client, org_id, plan["planning_job_id"])

    sources_by_unit = {item["cluster_id"]: _unit_sources(item, merged_from) for item in items}

    report_ids = {item["phase_cluster_id"] for item in items} | {
        source for sources in sources_by_unit.values() for source in sources
    }
    reports = {
        row["cluster_id"]: row
        for row in _fetch_in_chunks(
            lambda ids: client.table("cluster_reports")
            .select("cluster_id, summary_text, risk_level, grounding_status")
            .eq("org_id", org_id)
            .in_("cluster_id", ids),
            sorted(report_ids),
        )
    }

    facade_cycles = _open_facade_cycles(client, org_id, plan, _item_of_cluster(items))
    facade_units = {end for c in facade_cycles if c["level"] == "unit" for end in c["ends"]}

    approvals = {
        row["phase_cluster_id"]: row
        for row in (
            client.table("phase_approvals")
            .select("phase_cluster_id, status, note, reviewer_id, created_at")
            .eq("org_id", org_id)
            .eq("plan_id", plan["id"])
            .execute()
        ).data
    }

    phases: dict[str, dict] = {}
    for item in sorted(items, key=lambda i: (i["phase_order"], i["unit_order"])):
        phase_id = item["phase_cluster_id"]
        if phase_id not in phases:
            phases[phase_id] = {"phase_order": item["phase_order"], "units": []}
        unit_id = item["cluster_id"]
        sources = sources_by_unit[unit_id]
        phases[phase_id]["units"].append(
            {
                "cluster_id": unit_id,
                "unit_order": item["unit_order"],
                **_unit_fields(unit_id, sources, clusters, reports),
                "flags": _unit_flags(unit_id, sources, clusters, facade_units, _item_cluster_ids(item)),
            }
        )

    return {
        "id": plan["id"],
        "project_id": plan["project_id"],
        "version": plan["version"],
        "status": plan["status"],
        "created_at": plan.get("created_at"),
        "source_job_id": plan["source_job_id"],
        "is_stale": plan["is_stale"],
        "open_facade_flags": plan["open_facade_flags"],
        "approved_by": plan.get("approved_by"),
        "approved_at": plan.get("approved_at"),
        # The version this one was made from by an override (§9.2); null for version 1.
        "parent_plan_id": plan.get("parent_plan_id"),
        "parent_version": _parent_version(client, org_id, plan),
        # Open cycles a `facade_ack` override can acknowledge, in (level, scc_id) order.
        "facade_cycles": [
            {"level": c["level"], "scc_id": c["scc_id"], "cluster_ids": c["cluster_ids"]} for c in facade_cycles
        ],
        "phases": [
            _phase_overview(phase_id, phase, clusters, reports, approvals.get(phase_id))
            for phase_id, phase in phases.items()
        ],
    }


def _parent_version(client, org_id: str, plan: dict) -> int | None:
    if not plan.get("parent_plan_id"):
        return None
    rows = (
        client.table("migration_plans")
        .select("version")
        .eq("org_id", org_id)
        .eq("id", plan["parent_plan_id"])
        .limit(1)
        .execute()
    ).data
    return rows[0]["version"] if rows else None


def _item_cluster_ids(item: dict) -> list[str]:
    """The post-merge units (§7) a plan item stands for: what its members and stored
    unit edges hang off."""
    return (item.get("test_contract") or {}).get("merged_cluster_ids") or [item["cluster_id"]]


def _item_of_cluster(items: list[dict]) -> dict[str, str]:
    return {cluster_id: item["cluster_id"] for item in items for cluster_id in _item_cluster_ids(item)}


def _open_facade_cycles(client, org_id: str, plan: dict, item_of: dict[str, str]) -> list[dict]:
    """Still-open needs_facade SCCs (§7), both levels: not acknowledged by a
    `facade_ack` in this version's override chain and, at unit level, not folded into a
    single plan item by an override merge (§9.2). Each is `{level, scc_id, ends,
    cluster_ids}`: `ends` are the stored edge ends, `cluster_ids` the plan items (unit
    level) or phases (phase level) they belong to."""
    acked = acknowledged_sccs(load_chain(client, org_id, plan)[1]) if plan.get("parent_plan_id") else set()
    ends_by_scc: dict[tuple[str, int | None], set[str]] = {}
    for edge in _fetch_all(
        lambda: client.table("cluster_edges")
        .select("id, level, from_cluster_id, to_cluster_id, scc_id")
        .eq("org_id", org_id)
        .eq("planning_job_id", plan["planning_job_id"])
        .eq("flag", "needs_facade")
    ):
        scc_id = edge.get("scc_id")
        key = (edge["level"], int(scc_id) if scc_id is not None else None)
        ends_by_scc.setdefault(key, set()).update((edge["from_cluster_id"], edge["to_cluster_id"]))
    cycles = []
    for (level, scc_id), ends in sorted(ends_by_scc.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or 0)):
        if (level, scc_id) in acked:
            continue
        cluster_ids = sorted({item_of.get(end, end) for end in ends}) if level == "unit" else sorted(ends)
        if len(cluster_ids) < 2:
            continue
        cycles.append({"level": level, "scc_id": scc_id, "ends": ends, "cluster_ids": cluster_ids})
    return cycles


def _open_facade_units(client, org_id: str, plan: dict, item_of: dict[str, str]) -> set[str]:
    """Units at the ends of a still-open unit-level needs_facade SCC."""
    return {
        end for c in _open_facade_cycles(client, org_id, plan, item_of) if c["level"] == "unit" for end in c["ends"]
    }


def _unit_fields(unit_id: str, sources: list[str], clusters: dict, reports: dict) -> dict:
    cluster = clusters.get(unit_id) or {}
    if sources == [unit_id]:
        report = reports.get(unit_id) or {}
        return {
            "label": cluster.get("label"),
            "size": cluster.get("size"),
            "risk_level": report.get("risk_level"),
            "grounding_status": report.get("grounding_status"),
        }

    source_reports = [reports[s] for s in sources if s in reports]
    labels = sorted(clusters[s]["label"] for s in sources if clusters.get(s, {}).get("label"))
    if cluster.get("kind") == "merged_scc":
        size = cluster.get("size")
        label = cluster.get("label") or (" + ".join(labels) if labels else None)
    else:  # override merge: the item stands for every listed unit, so it names them all
        size = sum(clusters.get(s, {}).get("size") or 0 for s in sources)
        label = " + ".join(labels) if labels else cluster.get("label")
    return {
        "label": label,
        "size": size,
        "risk_level": rollup_risk([(r["cluster_id"], r["risk_level"]) for r in source_reports])[0]
        if source_reports
        else None,
        "grounding_status": _combined_grounding(source_reports, expected=len(sources)),
    }


def _combined_grounding(reports: list[dict], expected: int) -> str | None:
    if any(r["grounding_status"] == "unverified" for r in reports):
        return "unverified"
    if reports and len(reports) == expected:
        return "verified"
    return None


def _unit_flags(
    unit_id: str, sources: list[str], clusters: dict, facade_units: set[str], item_cluster_ids: list[str]
) -> list[str]:
    flags = []
    if (clusters.get(unit_id) or {}).get("kind") == "foundation":
        flags.append("foundation")
    if sources != [unit_id]:
        flags.append("merged")
    if facade_units & {unit_id, *sources, *item_cluster_ids}:
        flags.append("needs_facade")
    return flags


def _phase_overview(phase_id: str, phase: dict, clusters: dict, reports: dict, approval: dict | None) -> dict:
    report = reports.get(phase_id) or {}
    risk_level = report.get("risk_level")
    if risk_level is None:
        unit_risks = [(u["cluster_id"], u["risk_level"]) for u in phase["units"] if u["risk_level"]]
        risk_level = rollup_risk(unit_risks)[0] if unit_risks else None
    return {
        "phase_cluster_id": phase_id,
        "phase_order": phase["phase_order"],
        "label": (clusters.get(phase_id) or {}).get("label"),
        "summary_text": report.get("summary_text"),
        "risk_level": risk_level,
        "grounding_status": report.get("grounding_status"),
        "approval": {
            "status": approval["status"],
            "note": approval.get("note"),
            "reviewer_id": approval["reviewer_id"],
            "created_at": approval["created_at"],
        }
        if approval
        else None,
        "units": phase["units"],
    }


def _merged_from(client, org_id: str, planning_job_id: str) -> dict[str, list[str]]:
    """merged_scc unit id -> its original units, inverted from graph_stats.merged_into (§7)."""
    rows = (
        client.table("planning_jobs")
        .select("graph_stats")
        .eq("org_id", org_id)
        .eq("id", planning_job_id)
        .limit(1)
        .execute()
    ).data
    merged_into = ((rows[0].get("graph_stats") if rows else None) or {}).get("merged_into") or {}
    merged_from: dict[str, list[str]] = {}
    for original, merged in sorted(merged_into.items()):
        merged_from.setdefault(merged, []).append(original)
    return merged_from


@router.get("/plans/{plan_id}/units/{cluster_id}")
async def get_plan_unit(plan_id: str, cluster_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    unit = build_unit_detail(auth.client, auth.org_id, plan, cluster_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found in this plan")
    return unit


def build_unit_detail(client, org_id: str, plan: dict, cluster_id: str) -> dict | None:
    """design_agent2.md §11 `GET /plans/{plan_id}/units/{cluster_id}`, or None when
    `cluster_id` isn't a plan item of this plan.

    A unit without a report of its own gets `report: null` and its constituents'
    reports in `constituent_reports` (§9.2: the UI shows those rather than one).
    """
    rows = (
        client.table("migration_plan_items")
        .select("cluster_id, phase_cluster_id, phase_order, unit_order, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", plan["id"])
        .eq("cluster_id", cluster_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return None
    item = rows[0]
    test_contract = item.get("test_contract") or {}

    sources = _unit_sources(item, _merged_from(client, org_id, plan["planning_job_id"]))
    # Members live on the post-merge clusters: a merged_scc unit holds its originals'
    # members (§7), and an override merge spans every listed unit.
    member_cluster_ids = test_contract.get("merged_cluster_ids") or [cluster_id]
    unit_cluster_ids = sorted({cluster_id, *sources, *member_cluster_ids})

    clusters = {
        row["id"]: row
        for row in _fetch_in_chunks(
            lambda ids: client.table("clusters")
            .select("id, level, kind, size, label")
            .eq("org_id", org_id)
            .eq("planning_job_id", plan["planning_job_id"])
            .in_("id", ids),
            unit_cluster_ids,
        )
    }
    reports = {
        row["cluster_id"]: row
        for row in _fetch_in_chunks(
            lambda ids: client.table("cluster_reports")
            .select("cluster_id, summary_text, risk_level, risk_factors, grounding_status")
            .eq("org_id", org_id)
            .in_("cluster_id", ids),
            unit_cluster_ids,
        )
    }
    edges = _incident_unit_edges(client, org_id, plan["planning_job_id"], unit_cluster_ids)
    all_items = _fetch_all(
        lambda: client.table("migration_plan_items")
        .select("cluster_id, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", plan["id"]),
        order_column="cluster_id",
    )
    facade_units = _open_facade_units(client, org_id, plan, _item_of_cluster(all_items))

    own_report = sources == [cluster_id]
    return {
        "plan_id": plan["id"],
        "cluster_id": cluster_id,
        "phase_cluster_id": item["phase_cluster_id"],
        "phase_order": item["phase_order"],
        "unit_order": item["unit_order"],
        "kind": (clusters.get(cluster_id) or {}).get("kind"),
        **_unit_fields(cluster_id, sources, clusters, reports),
        "flags": _unit_flags(cluster_id, sources, clusters, facade_units, member_cluster_ids),
        "report": _report_detail(reports.get(cluster_id)) if own_report else None,
        "constituent_reports": []
        if own_report
        else [
            {"cluster_id": s, "label": (clusters.get(s) or {}).get("label"), **_report_detail(reports.get(s))}
            for s in sources
        ],
        "members": _unit_members(client, org_id, plan["source_job_id"], member_cluster_ids),
        "test_contract": test_contract,
        "edges": [
            {
                "id": e["id"],
                "from_cluster_id": e["from_cluster_id"],
                "to_cluster_id": e["to_cluster_id"],
                "direction": _edge_direction(e, set(unit_cluster_ids)),
                "call_count": e["call_count"],
                "hook_binding_count": e["hook_binding_count"],
                "weight": e["weight"],
                "flag": e["flag"],
                "scc_id": e.get("scc_id"),
            }
            for e in edges
        ],
    }


def _report_detail(report: dict | None) -> dict:
    report = report or {}
    return {
        "summary_text": report.get("summary_text"),
        "risk_level": report.get("risk_level"),
        "risk_factors": report.get("risk_factors") or [],
        "grounding_status": report.get("grounding_status"),
    }


def _incident_unit_edges(client, org_id: str, planning_job_id: str, cluster_ids: list[str]) -> list[dict]:
    """Unit-level `cluster_edges` with either end in `cluster_ids`, including the
    `merged` edges kept between a merged_scc unit's originals (§7)."""
    by_id = {}
    for end in ("from_cluster_id", "to_cluster_id"):
        for row in _fetch_in_chunks(
            lambda ids: client.table("cluster_edges")
            .select("id, from_cluster_id, to_cluster_id, call_count, hook_binding_count, weight, flag, scc_id")
            .eq("org_id", org_id)
            .eq("planning_job_id", planning_job_id)
            .eq("level", "unit")
            .in_(end, ids),
            cluster_ids,
        ):
            by_id[row["id"]] = row
    return sorted(by_id.values(), key=lambda e: (e["from_cluster_id"], e["to_cluster_id"], e["id"]))


def _edge_direction(edge: dict, unit_cluster_ids: set[str]) -> str:
    from_inside = edge["from_cluster_id"] in unit_cluster_ids
    to_inside = edge["to_cluster_id"] in unit_cluster_ids
    if from_inside and to_inside:
        return "internal"
    return "outgoing" if from_inside else "incoming"


def _unit_members(client, org_id: str, source_job_id: str, member_cluster_ids: list[str]) -> list[dict]:
    """One row per canonical entity (§3.2), since duplicates are the same code. Its
    grounding is the representative's summary, else any duplicate's (as in §10)."""
    rows = _fetch_all(
        lambda: client.table("cluster_members")
        .select("entity_id, canonical_entity_id")
        .eq("org_id", org_id)
        .in_("cluster_id", member_cluster_ids),
        order_column="entity_id",
    )
    duplicates: dict[str, list[str]] = {}
    for row in rows:
        duplicates.setdefault(row["canonical_entity_id"], []).append(row["entity_id"])
    if not duplicates:
        return []

    entities = _fetch_in_chunks(
        lambda ids: client.table("extracted_entities")
        .select("id, entity_type, name, file_path")
        .eq("org_id", org_id)
        .in_("id", ids),
        sorted(duplicates),
    )
    all_entity_ids = sorted({e for ids in duplicates.values() for e in ids} | set(duplicates))
    grounding = {
        row["entity_id"]: row["grounding_status"]
        for row in _fetch_in_chunks(
            lambda ids: client.table("knowledge_summaries")
            .select("entity_id, grounding_status")
            .eq("org_id", org_id)
            .eq("job_id", source_job_id)
            .eq("level", "entity")
            .in_("entity_id", ids),
            all_entity_ids,
        )
    }

    members = []
    for entity in entities:
        candidates = [entity["id"], *sorted(duplicates[entity["id"]])]
        members.append(
            {
                "id": entity["id"],
                "entity_type": entity["entity_type"],
                "name": entity["name"],
                "file_path": entity["file_path"],
                "grounding_status": next((grounding[e] for e in candidates if e in grounding), None),
            }
        )
    return sorted(members, key=lambda m: (m["file_path"] or "", m["name"] or "", m["entity_type"], m["id"]))


@router.get("/plans/{plan_id}/dependencies")
async def get_plan_dependencies(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    """design_agent2.md §11: Agent 1's declared packages for the plan's source job (facts), with the
    proposed target from the root (version 1) plan where there is one (inference)."""
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan_id": plan_id, "dependencies": build_plan_dependencies(auth.client, auth.org_id, plan)}


def _root_plan_id(client, org_id: str, plan: dict) -> str:
    """Follows `parent_plan_id` to version 1, whose id the targets hang off (overrides never change packages)."""
    current = plan
    for _ in range(1000):  # a chain is as long as the override count; the bound only stops a corrupt cycle
        parent_id = current.get("parent_plan_id")
        if not parent_id:
            return current["id"]
        rows = (
            client.table("migration_plans")
            .select("id, parent_plan_id")
            .eq("org_id", org_id)
            .eq("id", parent_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            return current["id"]
        current = rows[0]
    return current["id"]


def build_plan_dependencies(client, org_id: str, plan: dict) -> list[dict]:
    entities = [
        row
        for row in _fetch_all(
            lambda: client.table("extracted_entities")
            .select("id, entity_type, name, raw_facts")
            .eq("org_id", org_id)
            .eq("job_id", plan["source_job_id"])
        )
        if row["entity_type"] == "dependency"
    ]
    root_id = _root_plan_id(client, org_id, plan)
    targets = {
        row["entity_id"]: row
        for row in _fetch_all(
            lambda: client.table("plan_dependency_targets")
            .select("entity_id, target_name, rationale, grounding_status")
            .eq("org_id", org_id)
            .eq("plan_id", root_id),
            order_column="entity_id",
        )
    }
    rows = []
    for entity in entities:
        facts = entity.get("raw_facts") or {}
        target = targets.get(entity["id"])
        rows.append(
            {
                "entity_id": entity["id"],
                "name": entity["name"],
                "ecosystem": facts.get("ecosystem"),
                "version_spec": facts.get("version_spec"),
                "manifest_path": facts.get("manifest_path"),
                "dev": bool(facts.get("dev")),
                "target_name": target["target_name"] if target else None,
                "rationale": target["rationale"] if target else None,
                "grounding_status": target["grounding_status"] if target else None,
            }
        )
    return sorted(rows, key=lambda r: (r["manifest_path"] or "", r["ecosystem"] or "", r["name"]))


@router.get("/plans/{plan_id}/graph")
async def get_plan_graph(
    plan_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    level: Annotated[Literal["phase", "unit"], Query()],
    phase_id: Annotated[str | None, Query()] = None,
):
    if phase_id is not None and level != "unit":
        raise HTTPException(status_code=422, detail="phase_id only applies to level=unit")
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    graph = build_plan_graph(auth.client, auth.org_id, plan, level, phase_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="phase not found in this plan")
    return graph


def build_plan_graph(client, org_id: str, plan: dict, level: str, phase_id: str | None) -> dict | None:
    """design_agent2.md §11 `GET /plans/{plan_id}/graph`. Nodes come from the plan
    overview (same labels, sizes and risk), in plan order. None if `phase_id` isn't a
    phase of this plan."""
    overview = build_plan_overview(client, org_id, plan)
    phases = overview["phases"]
    if phase_id is not None:
        phases = [p for p in phases if p["phase_cluster_id"] == phase_id]
        if not phases:
            return None

    if level == "phase":
        nodes = [
            {
                "id": p["phase_cluster_id"],
                "label": p["label"],
                "size": sum(u["size"] or 0 for u in p["units"]),
                "risk_level": p["risk_level"],
            }
            for p in phases
        ]
        node_of = {n["id"]: n["id"] for n in nodes}
    else:
        nodes = [
            {"id": u["cluster_id"], "label": u["label"], "size": u["size"], "risk_level": u["risk_level"]}
            for p in phases
            for u in p["units"]
        ]
        node_of = _plan_item_of_cluster(client, org_id, plan, {n["id"] for n in nodes})

    rows = _fetch_all(
        lambda: client.table("cluster_edges")
        .select("id, from_cluster_id, to_cluster_id, weight, flag")
        .eq("org_id", org_id)
        .eq("planning_job_id", plan["planning_job_id"])
        .eq("level", level)
    )
    edges: dict[tuple[str, str], dict] = {}
    for row in rows:
        a, b = node_of.get(row["from_cluster_id"]), node_of.get(row["to_cluster_id"])
        if a is None or b is None or a == b:  # outside the graph, or inside one merged node
            continue
        edge = edges.setdefault((a, b), {"from": a, "to": b, "weight": 0.0, "flag": "none"})
        edge["weight"] += row["weight"] or 0.0
        if _FLAG_PRIORITY.get(row["flag"], 0) > _FLAG_PRIORITY[edge["flag"]]:
            edge["flag"] = row["flag"]
    return {"nodes": nodes, "edges": [edges[k] for k in sorted(edges)]}


def _plan_item_of_cluster(client, org_id: str, plan: dict, item_ids: set[str]) -> dict[str, str]:
    """Unit cluster id -> the plan item (graph node) standing for it. Stored unit edges
    may end on a merged_scc unit's originals (§7) or on a unit folded into an override
    merge (§9.2); both map onto the item that absorbed them."""
    items = _fetch_all(
        lambda: client.table("migration_plan_items")
        .select("cluster_id, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", plan["id"]),
        order_column="cluster_id",
    )
    merged_from = _merged_from(client, org_id, plan["planning_job_id"])
    node_of: dict[str, str] = {}
    for item in items:
        item_id = item["cluster_id"]
        if item_id not in item_ids:
            continue
        for cluster_id in {item_id, *((item.get("test_contract") or {}).get("merged_cluster_ids") or [])}:
            node_of[cluster_id] = item_id
            for original in merged_from.get(cluster_id, []):
                node_of[original] = item_id
    return node_of


@router.post("/plans/{plan_id}/submit")
async def submit_plan(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan["is_stale"]:
        raise HTTPException(
            status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan"
        )
    if plan["status"] != "draft":
        raise HTTPException(
            status_code=409, detail=f"only a draft plan can be submitted for review (status={plan['status']})"
        )

    # Service-role: migration_plans has no authenticated update policy (§2). Filtering
    # on status too makes a concurrent submit (or stale check) a no-op, not a double move.
    updated = (
        get_service_role_client()
        .table("migration_plans")
        .update({"status": "in_review"})
        .eq("org_id", auth.org_id)
        .eq("id", plan_id)
        .eq("status", "draft")
        .execute()
    ).data
    if not updated:
        raise HTTPException(status_code=409, detail="plan is no longer a draft; reload it")
    return {"plan_id": plan_id, "status": "in_review"}


@router.post("/plans/{plan_id}/phases/{phase_cluster_id}/approval")
async def approve_phase(
    plan_id: str,
    phase_cluster_id: str,
    body: PhaseApprovalRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
):
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    phase_ids = _plan_phase_ids(auth.client, auth.org_id, plan_id)
    if phase_cluster_id not in phase_ids:
        raise HTTPException(status_code=404, detail="phase not found in this plan")
    if plan["is_stale"]:
        raise HTTPException(
            status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan"
        )
    if plan["status"] != "in_review":
        raise HTTPException(
            status_code=409, detail=f"phases can only be approved or rejected in review (status={plan['status']})"
        )

    approval = _record_phase_approval(auth, plan_id, phase_cluster_id, body)
    status = _apply_review_transitions(auth, plan, phase_ids)
    return {
        "plan_id": plan_id,
        "phase_cluster_id": phase_cluster_id,
        "approval": {
            "status": approval["status"],
            "note": approval.get("note"),
            "reviewer_id": approval["reviewer_id"],
            "created_at": approval.get("created_at"),
        },
        "status": status,
    }


def _plan_phase_ids(client, org_id: str, plan_id: str) -> set[str]:
    items = _fetch_all(
        lambda: client.table("migration_plan_items")
        .select("cluster_id, phase_cluster_id")
        .eq("org_id", org_id)
        .eq("plan_id", plan_id),
        order_column="cluster_id",
    )
    return {item["phase_cluster_id"] for item in items}


def _record_phase_approval(auth: AuthContext, plan_id: str, phase_cluster_id: str, body: PhaseApprovalRequest) -> dict:
    """Upserts this phase's decision. A first decision is inserted on the caller's
    RLS-scoped client, so the insert policy itself enforces `org_id` and
    `reviewer_id = auth.uid()`. There's no authenticated update policy, so changing an
    existing decision goes through the service-role client, filtered by id + org_id."""
    decision = {"status": body.status, "note": body.note, "reviewer_id": auth.user_id}
    existing = (
        auth.client.table("phase_approvals")
        .select("id")
        .eq("org_id", auth.org_id)
        .eq("plan_id", plan_id)
        .eq("phase_cluster_id", phase_cluster_id)
        .limit(1)
        .execute()
    ).data
    if not existing:
        row = {"plan_id": plan_id, "phase_cluster_id": phase_cluster_id, "org_id": auth.org_id, **decision}
        try:
            return auth.client.table("phase_approvals").insert(row).execute().data[0]
        except APIError as exc:
            if exc.code != _UNIQUE_VIOLATION:  # a concurrent first decision won the insert
                raise
            existing = (
                auth.client.table("phase_approvals")
                .select("id")
                .eq("org_id", auth.org_id)
                .eq("plan_id", plan_id)
                .eq("phase_cluster_id", phase_cluster_id)
                .limit(1)
                .execute()
            ).data
    return (
        get_service_role_client()
        .table("phase_approvals")
        .update({**decision, "updated_at": datetime.now(UTC).isoformat()})
        .eq("org_id", auth.org_id)
        .eq("id", existing[0]["id"])
        .execute()
    ).data[0]


def _apply_review_transitions(auth: AuthContext, plan: dict, phase_ids: set[str]) -> str:
    """§9.1: any rejected phase makes the plan `rejected`; every phase approved with
    `open_facade_flags = 0` makes it `approved`. Returns the plan's resulting status.
    The update only matches an `in_review` plan, so a concurrent change (a stale check,
    another reviewer's transition) wins, and its status is what's reported."""
    decisions = {
        row["phase_cluster_id"]: row["status"]
        for row in (
            auth.client.table("phase_approvals")
            .select("phase_cluster_id, status")
            .eq("org_id", auth.org_id)
            .eq("plan_id", plan["id"])
            .execute()
        ).data
    }
    if "rejected" in decisions.values():
        change = {"status": "rejected"}
    elif all(decisions.get(p) == "approved" for p in phase_ids) and plan["open_facade_flags"] == 0:
        change = {"status": "approved", "approved_by": auth.user_id, "approved_at": datetime.now(UTC).isoformat()}
    else:
        return "in_review"

    updated = (
        get_service_role_client()
        .table("migration_plans")
        .update(change)
        .eq("org_id", auth.org_id)
        .eq("id", plan["id"])
        .eq("status", "in_review")
        .execute()
    ).data
    if updated:
        return change["status"]
    current = load_plan(auth.client, auth.org_id, plan["id"])
    return current["status"] if current else change["status"]


@router.post("/plans/{plan_id}/overrides", status_code=201)
async def create_override(
    plan_id: str,
    body: PlanOverrideRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
):
    plan = load_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan["is_stale"]:
        raise HTTPException(
            status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan"
        )
    if plan["status"] not in ("draft", "in_review"):
        raise HTTPException(
            status_code=409,
            detail=f"overrides can only be made on a draft or in-review plan (status={plan['status']})",
        )

    # §9.2: rebuild this version from version 1 by replaying its override chain, then
    # apply the new override. Nothing is written until the new version is known valid.
    root, chain = load_chain(auth.client, auth.org_id, plan)
    base = load_base(auth.client, auth.org_id, root["id"], plan["planning_job_id"])
    state = replay(base, chain)
    try:
        apply_override(base, state, body.kind, body.payload)
        items, open_flags = build_items(base, state)
    except OverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_plan = _insert_plan_version(auth.org_id, plan, open_flags)
    service = get_service_role_client()
    try:
        insert_many_scoped(service, "migration_plan_items", auth.org_id, [{"plan_id": new_plan["id"], **i} for i in items])
        # On the caller's RLS-scoped client: the insert policy enforces org_id and
        # created_by = auth.uid(), same as phase_approvals.
        override = (
            auth.client.table("plan_overrides")
            .insert(
                {
                    "org_id": auth.org_id,
                    "plan_id": plan_id,
                    "kind": body.kind,
                    "payload": body.payload,
                    "created_by": auth.user_id,
                    "result_plan_id": new_plan["id"],
                }
            )
            .execute()
        ).data[0]
    except Exception:
        # A version with no override row can't be replayed, so it must not survive.
        service.table("migration_plan_items").delete().eq("org_id", auth.org_id).eq("plan_id", new_plan["id"]).execute()
        service.table("migration_plans").delete().eq("org_id", auth.org_id).eq("id", new_plan["id"]).execute()
        raise

    return {
        "plan_id": new_plan["id"],
        "version": new_plan["version"],
        "parent_plan_id": plan_id,
        "parent_version": plan["version"],
        "override_id": override["id"],
        "status": new_plan["status"],
        "open_facade_flags": open_flags,
    }


def _insert_plan_version(org_id: str, parent: dict, open_facade_flags: int) -> dict:
    """The next version number of this planning job (unique per job), as a `draft`
    child of `parent`. Service-role: migration_plans has no authenticated insert policy.
    A concurrent override taking the same number retries with the next one."""
    service = get_service_role_client()
    for _ in range(_VERSION_RETRIES):
        latest = (
            service.table("migration_plans")
            .select("version")
            .eq("org_id", org_id)
            .eq("planning_job_id", parent["planning_job_id"])
            .order("version", desc=True)
            .limit(1)
            .execute()
        ).data
        row = {
            "project_id": parent["project_id"],
            "planning_job_id": parent["planning_job_id"],
            "source_job_id": parent["source_job_id"],
            "version": (latest[0]["version"] if latest else 0) + 1,
            "parent_plan_id": parent["id"],
            "status": "draft",
            "open_facade_flags": open_facade_flags,
        }
        try:
            return insert_scoped(service, "migration_plans", org_id, row).data[0]
        except APIError as exc:
            if exc.code != _UNIQUE_VIOLATION:
                raise
    raise HTTPException(status_code=409, detail="another override is creating a version right now; try again")


def _fetch_all(query_factory, order_column: str = "id") -> list[dict]:
    """Pages past PostgREST's row cap. `query_factory()` must return a fresh, fully
    filtered query each call."""
    rows: list[dict] = []
    start = 0
    while True:
        page = query_factory().order(order_column).range(start, start + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _fetch_in_chunks(query_for_ids, ids: list[str]) -> list[dict]:
    """`in_` filters over many ids, chunked to keep the request URL short."""
    rows: list[dict] = []
    for start in range(0, len(ids), _IN_CHUNK):
        rows.extend(query_for_ids(ids[start : start + _IN_CHUNK]).execute().data)
    return rows


@router.get("/planning-jobs/{planning_job_id}")
async def get_planning_job(planning_job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    job = get_planning_job_status(auth.client, auth.org_id, planning_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="planning job not found")
    return job


@router.get("/planning-jobs/{planning_job_id}/cost")
def get_planning_job_cost(planning_job_id: str, auth: Annotated[AuthContext, Depends(get_cost_operator)]):
    """Operator-only (404 for everyone else): LLM spend of one planning job, design.md §7.4.
    A list-price estimate, not the Azure invoice."""
    rows = (
        auth.client.table("planning_jobs")
        .select("id")
        .eq("org_id", auth.org_id)
        .eq("id", planning_job_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status_code=404, detail="planning job not found")
    return build_job_cost_report(get_service_role_client(), auth.org_id, "planning", planning_job_id)


def get_planning_job_status(client, org_id: str, planning_job_id: str) -> dict | None:
    """Single planning job's poll-able status, or None if not found/not this org's.

    `plan_id` is the v1 plan the worker emitted for this job (design_agent2.md §8),
    and stays null until the job is `done`. Later override versions share the same
    planning_job_id, so the lookup is pinned to version 1 rather than "any plan".
    """
    result = (
        client.table("planning_jobs")
        .select("id, project_id, status, progress_stage, error")
        .eq("org_id", org_id)
        .eq("id", planning_job_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]

    plan_id = None
    if row["status"] == "done":
        plans = (
            client.table("migration_plans")
            .select("id")
            .eq("org_id", org_id)
            .eq("planning_job_id", planning_job_id)
            .eq("version", 1)
            .limit(1)
            .execute()
        ).data
        plan_id = plans[0]["id"] if plans else None

    # Rebuilt field by field, never passed through (tasks.md's _entity_stub/get_project lesson).
    return {
        "planning_job_id": row["id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "progress_stage": row.get("progress_stage"),
        "error": row.get("error"),
        "plan_id": plan_id,
    }


def _get_project_row(client, org_id: str, project_id: str) -> dict | None:
    result = (
        client.table("projects")
        .select("id, latest_job_id")
        .eq("org_id", org_id)
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_source_job(client, org_id: str, project_id: str, job_id: str) -> dict | None:
    """The source ingestion job, only if it belongs to this org *and* this project."""
    result = (
        client.table("ingestion_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _has_active_planning_job(client, org_id: str, project_id: str) -> bool:
    rows = (
        client.table("planning_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    return any(row["status"] in _ACTIVE_STATUSES for row in rows)
