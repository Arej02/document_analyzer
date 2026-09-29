"""Agent 3 (Execute) API — design_agent3.md §9.

POST /api/agent3/plans/{plan_id}/executions creates an execution_jobs row
(status=queued) plus one pending unit_executions row per plan item, and
enqueues it on agent3_execution_jobs. Nothing here runs the pipeline inline —
see CLAUDE.md "Async by default".
GET /api/agent3/executions/{execution_job_id} is the matching status poll, and
GET /api/agent3/plans/{plan_id}/executions lists a plan's runs, newest first.

Plan, project and source-job lookups go through the caller's RLS-scoped client,
same as Agents 1 and 2. The execution_jobs / unit_executions inserts go through
the service-role client: neither table has an authenticated insert policy
(design_agent3.md §2 "writes are service-role only"), and org_id is stamped
from the server-resolved AuthContext, never from the request.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError

from ..auth import AuthContext, get_auth_context
from ..queue import enqueue_execution_job
from ..shared.plan_staleness import is_stale, mark_stale_plans
from ..shared.unit_sources import unit_sources
from ..supabase_client import get_service_role_client, insert_many_scoped, insert_scoped
from ..worker.snapshot import SNAPSHOT_BUCKET
from .worker.context import ContextError, _source_slice
from .worker.prepare import unpack_snapshot
from .worker.sandbox import SandboxParams
from .worker.status import TERMINAL_UNIT_STATUSES, counts_by_job

_OUTPUT_BUCKET = "agent3-output"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent3", tags=["agent3"])

_ACTIVE_STATUSES = ("queued", "running")

# Postgres unique_violation — execution_jobs_one_active_per_plan firing when two
# requests race past the active-job check below.
_UNIQUE_VIOLATION = "23505"

# PostgREST caps a response at 1000 rows; plan item reads page past it.
_PAGE_SIZE = 1000
# Ids per `in_` filter, so a cluster-label lookup's URL stays well under common limits.
_IN_CHUNK = 200

_ACTIVE_JOB_DETAIL = "code generation is already queued or running for this plan"


def execution_params_from_env() -> dict:
    """design_agent3.md §11 defaults, frozen into execution_jobs.params at creation so
    later env changes never alter an existing job. AGENT3_SANDBOX_RUNTIME and
    AGENT3_REQUIRE_GVISOR are operational, not frozen."""
    sandbox = SandboxParams.from_env()
    return {
        "model_deployment": os.environ.get("AGENT3_MODEL_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT"),
        "max_attempts": int(os.environ.get("AGENT3_MAX_ATTEMPTS", 3)),
        "max_context_chars": int(os.environ.get("AGENT3_MAX_CONTEXT_CHARS", 60000)),
        "sandbox_timeout_seconds": sandbox.timeout_seconds,
        "sandbox_memory_mb": sandbox.memory_mb,
        "runner_image": sandbox.image,
    }


@router.post("/plans/{plan_id}/executions", status_code=202)
async def create_execution(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan = _get_plan(auth.client, auth.org_id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")

    project = _get_project_row(auth.client, auth.org_id, plan["project_id"])
    latest_job_id = project.get("latest_job_id") if project else None
    mark_stale_plans(get_service_role_client(), auth.org_id, plan["project_id"], latest_job_id)
    if is_stale(plan["source_job_id"], latest_job_id) or plan["status"] == "stale":
        raise HTTPException(
            status_code=409, detail="plan is stale: it was built on an older ingestion run; generate a new plan"
        )
    if plan["status"] != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"only an approved plan can generate code; approve every phase first (status={plan['status']})",
        )

    source_job = _get_source_job(auth.client, auth.org_id, plan["source_job_id"])
    if source_job is None or not source_job.get("source_snapshot_path"):
        raise HTTPException(
            status_code=409,
            detail="this plan's ingestion run has no source snapshot; re-ingest this project to generate code",
        )

    items = _get_plan_items(auth.client, auth.org_id, plan_id)
    if not items:
        raise HTTPException(status_code=409, detail="plan has no units to generate")

    if _has_active_execution(auth.client, auth.org_id, plan_id):
        raise HTTPException(status_code=409, detail=_ACTIVE_JOB_DETAIL)

    service_client = get_service_role_client()
    row = {
        "project_id": plan["project_id"],
        "plan_id": plan_id,
        "source_job_id": plan["source_job_id"],
        "status": "queued",
        "params": execution_params_from_env(),
    }
    try:
        result = insert_scoped(service_client, "execution_jobs", auth.org_id, row)
    except APIError as exc:
        if exc.code == _UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail=_ACTIVE_JOB_DETAIL) from exc
        raise
    execution_job_id = result.data[0]["id"]

    unit_rows = [
        {
            "execution_job_id": execution_job_id,
            "cluster_id": item["cluster_id"],
            "phase_cluster_id": item["phase_cluster_id"],
            "phase_order": item["phase_order"],
            "unit_order": item["unit_order"],
            "status": "pending",
        }
        for item in items
    ]
    try:
        insert_many_scoped(service_client, "unit_executions", auth.org_id, unit_rows)
        await enqueue_execution_job(execution_job_id=execution_job_id, org_id=auth.org_id)
    except Exception:
        # A queued job nobody will run would block this plan forever
        # (execution_jobs_one_active_per_plan), so it's marked failed instead.
        logger.exception("could not start execution job %s", execution_job_id)
        _mark_not_started(service_client, auth.org_id, execution_job_id)
        raise

    return {"execution_job_id": execution_job_id, "status": "queued"}


def _mark_not_started(service_client, org_id: str, execution_job_id: str) -> None:
    try:
        (
            service_client.table("execution_jobs")
            .update({"status": "failed", "error": "could not start: creating units or enqueueing failed"})
            .eq("id", execution_job_id)
            .eq("org_id", org_id)
            .execute()
        )
    except Exception:  # noqa: BLE001 — the original error is the one to surface
        logger.exception("could not mark execution job %s failed", execution_job_id)


def _get_plan(client, org_id: str, plan_id: str) -> dict | None:
    result = (
        client.table("migration_plans")
        .select("id, project_id, source_job_id, status, planning_job_id")
        .eq("org_id", org_id)
        .eq("id", plan_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


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


def _get_source_job(client, org_id: str, job_id: str) -> dict | None:
    result = (
        client.table("ingestion_jobs")
        .select("id, source_snapshot_path")
        .eq("org_id", org_id)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_plan_items(client, org_id: str, plan_id: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("migration_plan_items")
            .select("cluster_id, phase_cluster_id, phase_order, unit_order")
            .eq("org_id", org_id)
            .eq("plan_id", plan_id)
            .order("cluster_id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        ).data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _has_active_execution(client, org_id: str, plan_id: str) -> bool:
    rows = (
        client.table("execution_jobs")
        .select("id, status")
        .eq("org_id", org_id)
        .eq("plan_id", plan_id)
        .in_("status", _ACTIVE_STATUSES)
        .execute()
    ).data
    return bool(rows)


@router.get("/executions/{execution_job_id}")
async def get_execution(execution_job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    job = get_execution_status(auth.client, auth.org_id, execution_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return job


def get_execution_status(client, org_id: str, execution_job_id: str) -> dict | None:
    """One execution job's poll-able status, or None if missing / not this org's.
    `counts` is computed from the job's unit rows on every call (design_agent3.md §9),
    so it moves while stage `units` runs."""
    result = (
        client.table("execution_jobs")
        .select("id, plan_id, project_id, status, progress_stage, error, final_test_summary, output_zip_path")
        .eq("org_id", org_id)
        .eq("id", execution_job_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]

    # Rebuilt field by field, never passed through (same rule as Agents 1 and 2).
    return {
        "execution_job_id": row["id"],
        "plan_id": row["plan_id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "progress_stage": row.get("progress_stage"),
        "error": row.get("error"),
        "counts": counts_by_job(client, org_id, [execution_job_id])[execution_job_id],
        "final_test_summary": row.get("final_test_summary"),
        "has_output": bool(row.get("output_zip_path")),
    }


@router.get("/plans/{plan_id}/executions")
async def list_executions(plan_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    if _get_plan(auth.client, auth.org_id, plan_id) is None:
        raise HTTPException(status_code=404, detail="plan not found")

    rows = (
        auth.client.table("execution_jobs")
        .select("id, status, progress_stage, created_at")
        .eq("org_id", auth.org_id)
        .eq("plan_id", plan_id)
        .order("created_at", desc=True)
        .execute()
    ).data
    counts = counts_by_job(auth.client, auth.org_id, [row["id"] for row in rows])
    return {
        "executions": [
            {
                "id": row["id"],
                "status": row["status"],
                "progress_stage": row.get("progress_stage"),
                "counts": counts[row["id"]],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


def _get_execution_job_plan_id(client, org_id: str, execution_job_id: str) -> str | None:
    result = (
        client.table("execution_jobs")
        .select("id, plan_id")
        .eq("org_id", org_id)
        .eq("id", execution_job_id)
        .limit(1)
        .execute()
    )
    return result.data[0]["plan_id"] if result.data else None


def _get_unit_executions(client, org_id: str, execution_job_id: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("unit_executions")
            .select("cluster_id, phase_cluster_id, phase_order, unit_order, status, attempts, flags, test_summary")
            .eq("org_id", org_id)
            .eq("execution_job_id", execution_job_id)
            .order("id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        ).data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _get_plan_item_contracts(client, org_id: str, plan_id: str) -> dict[str, dict]:
    """cluster_id -> test_contract, for `unit_sources`'s `merged_cluster_ids` check
    (design_agent2.md §7/§9.2) — `unit_executions` doesn't carry the contract itself."""
    contracts: dict[str, dict] = {}
    start = 0
    while True:
        page = (
            client.table("migration_plan_items")
            .select("cluster_id, test_contract")
            .eq("org_id", org_id)
            .eq("plan_id", plan_id)
            .order("cluster_id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        ).data
        for row in page:
            contracts[row["cluster_id"]] = row.get("test_contract") or {}
        if len(page) < _PAGE_SIZE:
            return contracts
        start += _PAGE_SIZE


def _merged_from(client, org_id: str, planning_job_id: str) -> dict[str, list[str]]:
    """merged_scc unit id -> its original unit ids, inverted from
    `planning_jobs.graph_stats.merged_into` (design_agent2.md §7). Same query
    `agent2/router.py::_merged_from` and `agent3/worker/prepare.py::_merged_from`
    run; kept as its own read here rather than a shared helper, since it touches
    the DB and Agent 3 is allowed to read Agent 2's tables directly (CLAUDE.md)."""
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


def _get_clusters(client, org_id: str, cluster_ids: list[str]) -> dict[str, dict]:
    clusters: dict[str, dict] = {}
    for i in range(0, len(cluster_ids), _IN_CHUNK):
        chunk = cluster_ids[i : i + _IN_CHUNK]
        rows = (
            client.table("clusters").select("id, kind, label").eq("org_id", org_id).in_("id", chunk).execute()
        ).data
        for row in rows:
            clusters[row["id"]] = row
    return clusters


def _unit_label(cluster_id: str, sources: list[str], clusters: dict) -> str | None:
    """`agent2/router.py::_unit_fields`'s label rule (design_agent2.md §11), the same
    one `agent3/worker/unit_slug.py::_unit_label` mirrors for `unit_slug` derivation —
    a third, router-local copy rather than importing the worker's (which reads
    `state.clusters`, not a plain dict, and isn't meant as a cross-module API)."""
    own_cluster = clusters.get(cluster_id) or {}
    if sources == [cluster_id]:
        return own_cluster.get("label")
    labels = sorted(clusters[s]["label"] for s in sources if clusters.get(s, {}).get("label"))
    if own_cluster.get("kind") == "merged_scc":
        return own_cluster.get("label") or (" + ".join(labels) if labels else None)
    return " + ".join(labels) if labels else own_cluster.get("label")  # override merge: always joins


@router.get("/executions/{execution_job_id}/units")
async def get_execution_units(execution_job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    plan_id = _get_execution_job_plan_id(auth.client, auth.org_id, execution_job_id)
    if plan_id is None:
        raise HTTPException(status_code=404, detail="execution not found")

    unit_rows = _get_unit_executions(auth.client, auth.org_id, execution_job_id)
    contracts = _get_plan_item_contracts(auth.client, auth.org_id, plan_id)
    plan = _get_plan(auth.client, auth.org_id, plan_id)
    merged_from = _merged_from(auth.client, auth.org_id, plan["planning_job_id"]) if plan else {}

    sources_by_cluster: dict[str, list[str]] = {}
    cluster_ids: set[str] = set()
    for row in unit_rows:
        cluster_id = row["cluster_id"]
        item = {"cluster_id": cluster_id, "test_contract": contracts.get(cluster_id, {})}
        sources = unit_sources(item, merged_from)
        sources_by_cluster[cluster_id] = sources
        cluster_ids.update([cluster_id, *sources])
    cluster_ids.update(row["phase_cluster_id"] for row in unit_rows)

    clusters = _get_clusters(auth.client, auth.org_id, sorted(cluster_ids))

    phases_by_id: dict[str, dict] = {}
    for row in unit_rows:
        phase_id = row["phase_cluster_id"]
        phase = phases_by_id.setdefault(
            phase_id,
            {
                "phase_cluster_id": phase_id,
                "phase_order": row["phase_order"],
                "label": clusters.get(phase_id, {}).get("label"),
                "units": [],
            },
        )
        cluster_id = row["cluster_id"]
        phase["units"].append(
            {
                "cluster_id": cluster_id,
                "unit_order": row["unit_order"],
                "label": _unit_label(cluster_id, sources_by_cluster[cluster_id], clusters),
                "status": row["status"],
                "attempts": row["attempts"],
                "flags": row.get("flags") or [],
                "test_summary": row.get("test_summary"),
            }
        )

    phases = sorted(phases_by_id.values(), key=lambda p: p["phase_order"])
    for phase in phases:
        phase["units"].sort(key=lambda u: u["unit_order"])

    return {"phases": phases}


def _get_unit_execution(client, org_id: str, execution_job_id: str, cluster_id: str) -> dict | None:
    result = (
        client.table("unit_executions")
        .select("cluster_id, status, attempts, flags, last_error, test_summary, symbol_map, storage_prefix")
        .eq("org_id", org_id)
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_plan_item(client, org_id: str, plan_id: str, cluster_id: str) -> dict | None:
    result = (
        client.table("migration_plan_items")
        .select("cluster_id, test_contract")
        .eq("org_id", org_id)
        .eq("plan_id", plan_id)
        .eq("cluster_id", cluster_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _member_entity_ids(client, org_id: str, cluster_id: str, merged_cluster_ids: list[str]) -> set[str]:
    """design_agent3.md §3 step 2's rule, for one unit only: `cluster_members` of its
    own cluster, plus (for an override merge) every id in `merged_cluster_ids`. A
    router-local copy of `agent3/worker/prepare.py::_member_entity_ids_by_item`'s core
    query — that function batches over every item in a plan (the worker's own need),
    this endpoint only ever needs one."""
    all_cluster_ids = sorted({cluster_id, *merged_cluster_ids})
    members: set[str] = set()
    for i in range(0, len(all_cluster_ids), _IN_CHUNK):
        chunk = all_cluster_ids[i : i + _IN_CHUNK]
        rows = (
            client.table("cluster_members")
            .select("cluster_id, canonical_entity_id")
            .eq("org_id", org_id)
            .in_("cluster_id", chunk)
            .execute()
        ).data
        members.update(row["canonical_entity_id"] for row in rows)
    return members


def _get_entities(client, org_id: str, source_job_id: str, entity_ids: list[str]) -> dict[str, dict]:
    entities: dict[str, dict] = {}
    for i in range(0, len(entity_ids), _IN_CHUNK):
        chunk = entity_ids[i : i + _IN_CHUNK]
        rows = (
            client.table("extracted_entities")
            .select("id, entity_type, name, file_path, raw_facts")
            .eq("org_id", org_id)
            .eq("job_id", source_job_id)
            .in_("id", chunk)
            .execute()
        ).data
        for row in rows:
            entities[row["id"]] = row
    return entities


def _build_legacy(entities: dict[str, dict], snapshot_dir: Path | None) -> list[dict]:
    """`_source_slice` (`agent3/worker/context.py`, imported — pure, no DB access, so
    reusing it here isn't an agent-call, CLAUDE.md) needs every member alongside the
    one it's slicing, for the class-header-on-a-method lookup. A per-member
    `ContextError` (a data problem in this one entity's `raw_facts`, e.g. an
    out-of-range span) degrades to no source for that member rather than failing the
    whole detail view — a display endpoint, unlike stage `units`, where the same error
    ends the unit `error` (CLAUDE.md "grounding failures never block" applies to
    reads too: one bad row shouldn't hide every other member's source)."""
    legacy = []
    for entity_id in sorted(entities):
        entity = entities[entity_id]
        raw_facts = entity.get("raw_facts") or {}
        source = None
        if snapshot_dir is not None:
            try:
                source = _source_slice(snapshot_dir, entities, entity)
            except ContextError:
                source = None
        legacy.append(
            {
                "entity_id": entity_id,
                "name": entity["name"],
                "file_path": entity["file_path"],
                "line": raw_facts.get("line"),
                "end_line": raw_facts.get("end_line"),
                "source": source,
            }
        )
    return legacy


def _list_objects(bucket, prefix: str) -> list[str]:
    """Every object path under `prefix` (with or without a trailing slash),
    recursively — same pattern as `projects.py::_list_objects`, kept as its own copy
    here (that one lives with project-delete's storage cleanup, a different module's
    concern)."""
    base = prefix.rstrip("/")
    paths: list[str] = []
    offset = 0
    while True:
        entries = bucket.list(base, {"limit": _PAGE_SIZE, "offset": offset})
        for entry in entries:
            path = f"{base}/{entry['name']}"
            if entry.get("id") is None:
                paths.extend(_list_objects(bucket, path))
            else:
                paths.append(path)
        if len(entries) < _PAGE_SIZE:
            return paths
        offset += _PAGE_SIZE


def _read_attempt_files(
    client, storage_prefix: str | None, attempts: int
) -> tuple[list[dict], list[dict], str | None]:
    """The last attempt's code, tests and log (design_agent3.md §2's object layout,
    `attempt_{attempts}` — `attempts` is the outer round count `units.py` persists,
    exactly matching the folder the last attempt was uploaded under). `storage_prefix`
    is `None` for a unit that never got a real attempt (`error` with no sandbox run,
    or still `pending`/`running`) — nothing was ever uploaded, so this returns empty."""
    if not storage_prefix:
        return [], [], None
    attempt_prefix = f"{storage_prefix}attempt_{attempts}/"
    bucket = client.storage.from_(_OUTPUT_BUCKET)

    code_prefix = f"{attempt_prefix}code/"
    files = [
        {"path": path[len(code_prefix) :], "content": bucket.download(path).decode("utf-8")}
        for path in sorted(_list_objects(bucket, code_prefix))
    ]
    tests_prefix = f"{attempt_prefix}tests/"
    tests = [
        # Stripped only to attempt_prefix, not tests_prefix: the original path (as
        # generate.py/tests_gen.py wrote it) already starts with "tests/...".
        {"path": path[len(attempt_prefix) :], "content": bucket.download(path).decode("utf-8")}
        for path in sorted(_list_objects(bucket, tests_prefix))
    ]
    try:
        log = bucket.download(f"{attempt_prefix}log.txt").decode("utf-8")
    except Exception:  # noqa: BLE001 — a missing/unreadable log degrades to None, not a 500
        log = None
    return files, tests, log


@router.get("/executions/{execution_job_id}/units/{cluster_id}")
async def get_execution_unit(
    execution_job_id: str, cluster_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]
):
    plan_id = _get_execution_job_plan_id(auth.client, auth.org_id, execution_job_id)
    if plan_id is None:
        raise HTTPException(status_code=404, detail="execution not found")

    unit_row = _get_unit_execution(auth.client, auth.org_id, execution_job_id, cluster_id)
    if unit_row is None:
        raise HTTPException(status_code=404, detail="unit not found in this execution")

    plan = _get_plan(auth.client, auth.org_id, plan_id)
    item = _get_plan_item(auth.client, auth.org_id, plan_id, cluster_id)
    merged_cluster_ids = (item.get("test_contract") or {}).get("merged_cluster_ids") or [] if item else []

    legacy: list[dict] = []
    if plan is not None:
        member_ids = _member_entity_ids(auth.client, auth.org_id, cluster_id, merged_cluster_ids)
        entities = _get_entities(auth.client, auth.org_id, plan["source_job_id"], sorted(member_ids))
        if entities:
            source_job = _get_source_job(auth.client, auth.org_id, plan["source_job_id"])
            snapshot_dir = None
            if source_job and source_job.get("source_snapshot_path"):
                # Re-downloaded and unpacked on every request, like the worker itself
                # does per job (prepare.py) — nothing in this system caches the
                # snapshot; a real perf concern for a large repo, but out of this
                # task's scope (no request volume yet to size it against).
                tar_bytes = auth.client.storage.from_(SNAPSHOT_BUCKET).download(source_job["source_snapshot_path"])
                snapshot_dir = Path(tempfile.mkdtemp(prefix="agent3-unit-detail-"))
                try:
                    unpack_snapshot(tar_bytes, snapshot_dir)
                    legacy = _build_legacy(entities, snapshot_dir)
                finally:
                    shutil.rmtree(snapshot_dir, ignore_errors=True)
            else:
                legacy = _build_legacy(entities, None)

    files, tests, log = _read_attempt_files(
        auth.client, unit_row.get("storage_prefix"), unit_row.get("attempts") or 0
    )

    return {
        "cluster_id": cluster_id,
        "status": unit_row["status"],
        "attempts": unit_row.get("attempts") or 0,
        "flags": unit_row.get("flags") or [],
        "last_error": unit_row.get("last_error"),
        "test_summary": unit_row.get("test_summary"),
        "symbol_map": unit_row.get("symbol_map"),
        "legacy": legacy,
        "files": files,
        "tests": tests,
        "log": log,
    }


# design_agent3.md §9: "a signed URL, 5 min".
_DOWNLOAD_EXPIRES_IN = 300


def _get_execution_job_output(client, org_id: str, execution_job_id: str) -> dict | None:
    result = (
        client.table("execution_jobs")
        .select("id, output_zip_path")
        .eq("org_id", org_id)
        .eq("id", execution_job_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


@router.get("/executions/{execution_job_id}/download")
async def get_execution_download(execution_job_id: str, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    job = _get_execution_job_output(auth.client, auth.org_id, execution_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="execution not found")
    if not job.get("output_zip_path"):
        raise HTTPException(status_code=409, detail="no output has been generated for this execution yet")

    bucket = auth.client.storage.from_(_OUTPUT_BUCKET)
    signed = bucket.create_signed_url(job["output_zip_path"], _DOWNLOAD_EXPIRES_IN)
    return {"url": signed["signedUrl"], "expires_in": _DOWNLOAD_EXPIRES_IN}
