"""Stage `prepare` — design_agent3.md §3.

Loads everything the rest of the execution job needs into the in-memory
`ExecutionState` (run.py): the plan (re-checked), its items in migration order,
each item's member entities and their summaries, and the unpacked source
snapshot. Also resumes a redelivered job by carrying forward already-terminal
units' symbol maps, real code/tests (or a regenerated stub) and `stubbed_units`
membership (§3 step 5, `_load_resume_state`), and attaches each item's
`unit_slug` (§8, `unit_slug.py`) so `generate.py` and `tests_gen.py` don't each
have to derive it.

Reads only (CLAUDE.md "Agent 2 is read-only on Agent 1 tables" — Agent 3 is
read-only on Agents 1 and 2 too), all through the worker's service-role client,
every query filtered by `org_id`. The snapshot tarball is untrusted input
(CLAUDE.md "untrusted input, no code execution"): it's unpacked, never
executed, and guarded against path traversal, symlink escapes and size bombs.
"""

import io
import shutil
import tarfile
import tempfile
from pathlib import Path

from supabase import Client

from document_analyzer.resource_bounds import MAX_FILE_COUNT, MAX_UPLOAD_SIZE_BYTES
from document_analyzer.shared.plan_staleness import is_stale, mark_stale_plans
from document_analyzer.shared.unit_sources import unit_sources
from document_analyzer.worker.snapshot import SNAPSHOT_BUCKET

from .generate import generate_stub_unit
from .status import TERMINAL_UNIT_STATUSES
from .unit_slug import derive_unit_slug
from .units import download_attempt_files, ensure_package_init

# PostgREST caps a response at 1000 rows; plan items and member rows are paged
# past it, following the pattern in router.py / agent2/worker/graph.py.
_PAGE_SIZE = 1000
# Ids per `in_` filter, so query URLs stay well under common limits.
_IN_CHUNK = 200


class PrepareError(Exception):
    """A readable, job-level failure raised by stage `prepare` (never a unit verdict)."""


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _fetch_all(query_factory, order_column: str = "id") -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = query_factory().order(order_column).range(start, start + _PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _load_plan(client: Client, org_id: str, plan_id: str) -> dict | None:
    result = (
        client.table("migration_plans")
        .select("id, project_id, source_job_id, status, planning_job_id")
        .eq("org_id", org_id)
        .eq("id", plan_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _load_project(client: Client, org_id: str, project_id: str) -> dict | None:
    result = (
        client.table("projects")
        .select("id, latest_job_id")
        .eq("org_id", org_id)
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _check_plan_still_valid(client: Client, org_id: str, plan: dict) -> None:
    """design_agent3.md §3 step 1: the plan may have gone stale or been un-approved
    (rejected) after the execution job was enqueued. `mark_stale_plans` is imported
    from `shared/` (CLAUDE.md: agents share only pure helpers, never each other's
    routers)."""
    project = _load_project(client, org_id, plan["project_id"])
    latest_job_id = project.get("latest_job_id") if project else None
    mark_stale_plans(client, org_id, plan["project_id"], latest_job_id)
    if is_stale(plan["source_job_id"], latest_job_id) or plan["status"] == "stale":
        raise PrepareError("plan went stale after code generation was queued")
    if plan["status"] != "approved":
        raise PrepareError(f"plan is no longer approved (status={plan['status']})")


def _load_items(client: Client, org_id: str, plan_id: str) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("migration_plan_items")
            .select("cluster_id, phase_cluster_id, phase_order, unit_order, test_contract")
            .eq("org_id", org_id)
            .eq("plan_id", plan_id)
            .order("cluster_id")
            .range(start, start + _PAGE_SIZE - 1)
            .execute()
        ).data
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    rows.sort(key=lambda r: (r["phase_order"], r["unit_order"]))
    return rows


def _cluster_member_ids(client: Client, org_id: str, cluster_ids: list[str]) -> dict[str, set[str]]:
    """cluster_id -> canonical entity ids of its cluster_members (§3 step 2). One row
    per canonical entity, same rule as the plan-detail endpoint (design_agent2.md §11)."""
    by_cluster: dict[str, set[str]] = {cid: set() for cid in cluster_ids}
    for chunk in _chunked(cluster_ids, _IN_CHUNK):
        rows = _fetch_all(
            lambda chunk=chunk: client.table("cluster_members")
            .select("cluster_id, entity_id, canonical_entity_id")
            .eq("org_id", org_id)
            .in_("cluster_id", chunk),
            order_column="entity_id",
        )
        for row in rows:
            by_cluster[row["cluster_id"]].add(row["canonical_entity_id"])
    return by_cluster


def _member_entity_ids_by_item(client: Client, org_id: str, items: list[dict]) -> dict[str, set[str]]:
    """§3 step 2: cluster_members of the item's own cluster, plus (for an override
    merge) every id in test_contract.merged_cluster_ids. A merged_scc unit's members
    are already re-pointed onto its cluster_id, so it needs no extra fetch."""
    all_cluster_ids = {item["cluster_id"] for item in items}
    for item in items:
        all_cluster_ids.update((item.get("test_contract") or {}).get("merged_cluster_ids") or [])

    members_by_cluster = _cluster_member_ids(client, org_id, sorted(all_cluster_ids))

    result: dict[str, set[str]] = {}
    for item in items:
        member_ids = set(members_by_cluster.get(item["cluster_id"], set()))
        for merged_id in (item.get("test_contract") or {}).get("merged_cluster_ids") or []:
            member_ids |= members_by_cluster.get(merged_id, set())
        result[item["cluster_id"]] = member_ids
    return result


def _load_entities(client: Client, org_id: str, source_job_id: str, entity_ids: list[str]) -> dict[str, dict]:
    entities: dict[str, dict] = {}
    for chunk in _chunked(sorted(entity_ids), _IN_CHUNK):
        rows = _fetch_all(
            lambda chunk=chunk: client.table("extracted_entities")
            .select("id, entity_type, name, file_path, raw_facts")
            .eq("org_id", org_id)
            .eq("job_id", source_job_id)
            .in_("id", chunk)
        )
        for row in rows:
            entities[row["id"]] = row
    return entities


def _load_summaries(client: Client, org_id: str, source_job_id: str, entity_ids: list[str]) -> dict[str, dict]:
    summaries: dict[str, dict] = {}
    for chunk in _chunked(sorted(entity_ids), _IN_CHUNK):
        rows = _fetch_all(
            lambda chunk=chunk: client.table("knowledge_summaries")
            .select("id, entity_id, summary_text, risk_level, grounding_status")
            .eq("org_id", org_id)
            .eq("job_id", source_job_id)
            .eq("level", "entity")
            .in_("entity_id", chunk)
        )
        for row in rows:
            summaries[row["entity_id"]] = row
    return summaries


def _merged_from(client: Client, org_id: str, planning_job_id: str) -> dict[str, list[str]]:
    """merged_scc unit id -> its original units, inverted from graph_stats.merged_into
    (design_agent2.md §7). Same query `agent2/router.py::_merged_from` runs; kept as its
    own read here rather than a shared helper, since (unlike `unit_sources`) it touches
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


def _load_cluster_reports(client: Client, org_id: str, cluster_ids: list[str]) -> dict[str, dict]:
    reports: dict[str, dict] = {}
    for chunk in _chunked(sorted(cluster_ids), _IN_CHUNK):
        rows = _fetch_all(
            lambda chunk=chunk: client.table("cluster_reports")
            .select("cluster_id, summary_text, risk_level, grounding_status")
            .eq("org_id", org_id)
            .in_("cluster_id", chunk),
            order_column="cluster_id",
        )
        for row in rows:
            reports[row["cluster_id"]] = row
    return reports


def _load_clusters(client: Client, org_id: str, cluster_ids: list[str]) -> dict[str, dict]:
    """cluster id -> {kind, label} for §8's `unit_slug` derivation (unit_slug.py):
    the same fields `agent2/router.py::_unit_fields` reads to build a merged unit's
    label, so a plan item gets the same slug whichever agent looks at it."""
    clusters: dict[str, dict] = {}
    for chunk in _chunked(sorted(cluster_ids), _IN_CHUNK):
        rows = _fetch_all(
            lambda chunk=chunk: client.table("clusters").select("id, kind, label").eq("org_id", org_id).in_(
                "id", chunk
            ),
        )
        for row in rows:
            clusters[row["id"]] = row
    return clusters


def _load_source_snapshot_path(client: Client, org_id: str, source_job_id: str) -> str:
    result = (
        client.table("ingestion_jobs")
        .select("id, source_snapshot_path")
        .eq("org_id", org_id)
        .eq("id", source_job_id)
        .limit(1)
        .execute()
    )
    row = result.data[0] if result.data else None
    if row is None or not row.get("source_snapshot_path"):
        raise PrepareError("this plan's ingestion run has no source snapshot")
    return row["source_snapshot_path"]


def unpack_snapshot(tar_bytes: bytes, dest_dir: Path) -> None:
    """Safely unpacks the (untrusted, but already-bounded-at-upload) source tarball.

    Two layers, because the tarball is decompressed here for the first time since
    `worker/snapshot.py` built it — a hostile object at this path could still be a
    decompression bomb:
    - a file-count / total-uncompressed-size cap (the same bound `worker/snapshot.py`
      enforces on the compressed archive, reused here on its *contents*), checked
      before any bytes are written to disk;
    - `filter="data"` (stdlib, Python 3.12+): refuses absolute paths, `..` traversal,
      device/fifo members, and symlinks/hardlinks that would resolve outside
      `dest_dir`.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILE_COUNT:
                raise PrepareError(f"source snapshot has {len(members)} entries, exceeding max allowed {MAX_FILE_COUNT}")
            total_size = sum(m.size for m in members if m.isreg())
            if total_size > MAX_UPLOAD_SIZE_BYTES:
                raise PrepareError(
                    f"source snapshot unpacks to {total_size} bytes, exceeding max allowed {MAX_UPLOAD_SIZE_BYTES} bytes"
                )
            archive.extractall(dest_dir, members=members, filter="data")
    except tarfile.TarError as exc:
        raise PrepareError(f"source snapshot could not be unpacked safely: {exc}") from exc


def _download_and_unpack_snapshot(client: Client, org_id: str, source_job_id: str) -> Path:
    object_path = _load_source_snapshot_path(client, org_id, source_job_id)
    tar_bytes = client.storage.from_(SNAPSHOT_BUCKET).download(object_path)
    dest_dir = Path(tempfile.mkdtemp(prefix="agent3-snapshot-"))
    try:
        unpack_snapshot(tar_bytes, dest_dir)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return dest_dir


def _load_resume_state(
    client: Client, org_id: str, execution_job_id: str, items_by_cluster_id: dict[str, dict]
) -> tuple[set[str], dict[str, dict], dict[str, list], dict[str, list], set[str]]:
    """§3 step 5: a redelivered message must not re-run a terminal unit, and its
    dependents (built later in the same or a later run) must see its *real* result,
    not fall back to a fresh stub. Returns terminal cluster ids, symbol maps,
    `unit_files`/`unit_test_files` and `stubbed_units` — the same four pieces of
    state `units.py` builds up live during a single uninterrupted run (design_agent3.md
    §4), reconstructed here for whatever finished in an *earlier* run of this job.

    Closes the gap this module used to document ("state.unit_files is not
    repopulated from storage on resume"): a terminal unit with a `storage_prefix`
    (`passed`/`failed_verification`, which always ran a real sandbox attempt) has its
    last attempt's code and tests read back from `agent3-output`
    (`units.py::download_attempt_files`); one with no `storage_prefix` (`error` with
    no sandbox run at all, §4 "Failed dependencies") has its stub regenerated instead
    — `generate_stub_unit` is pure and deterministic, so this produces exactly the
    same files `_finish_as_error` would have stored, without needing to have stored
    them anywhere."""
    rows = _fetch_all(
        lambda: client.table("unit_executions")
        .select("cluster_id, status, symbol_map, attempts, flags, storage_prefix")
        .eq("org_id", org_id)
        .eq("execution_job_id", execution_job_id)
    )
    terminal_rows = [row for row in rows if row["status"] in TERMINAL_UNIT_STATUSES]
    terminal_unit_ids = {row["cluster_id"] for row in terminal_rows}
    symbol_maps = {row["cluster_id"]: row["symbol_map"] for row in terminal_rows if row.get("symbol_map")}
    stubbed_units = {row["cluster_id"] for row in terminal_rows if "stubbed" in (row.get("flags") or [])}

    unit_files: dict[str, list] = {}
    unit_test_files: dict[str, list] = {}
    for row in terminal_rows:
        cluster_id = row["cluster_id"]
        if row.get("storage_prefix"):
            code_files, test_files = download_attempt_files(client, row["storage_prefix"], row["attempts"])
            unit_files[cluster_id] = code_files
            unit_test_files[cluster_id] = test_files
            continue
        item = items_by_cluster_id.get(cluster_id)
        if item is not None:
            stub = generate_stub_unit(item, item["unit_slug"])
            unit_files[cluster_id] = ensure_package_init(stub.files, item["unit_slug"])

    return terminal_unit_ids, symbol_maps, unit_files, unit_test_files, stubbed_units


def run_prepare_stage(client: Client, execution_job: dict, state) -> None:
    org_id = execution_job["org_id"]

    plan = _load_plan(client, org_id, execution_job["plan_id"])
    if plan is None:
        raise PrepareError("plan not found")
    _check_plan_still_valid(client, org_id, plan)
    state.plan = plan

    items = _load_items(client, org_id, plan["id"])
    member_ids_by_item = _member_entity_ids_by_item(client, org_id, items)
    merged_from = _merged_from(client, org_id, plan["planning_job_id"])
    report_source_ids: set[str] = set()
    for item in items:
        item["member_entity_ids"] = sorted(member_ids_by_item[item["cluster_id"]])
        # §4 step 1: a merged_scc or override-merged item has no report of its own
        # (design_agent2.md §7, §9.2); its sources' reports stand in.
        item["report_source_cluster_ids"] = unit_sources(item, merged_from)
        report_source_ids.update(item["report_source_cluster_ids"])
    state.items = items
    state.cluster_reports = _load_cluster_reports(client, org_id, sorted(report_source_ids))
    state.clusters = _load_clusters(
        client, org_id, sorted(report_source_ids | {item["cluster_id"] for item in items})
    )
    for item in items:
        item["unit_slug"] = derive_unit_slug(item, state)

    all_entity_ids: set[str] = set()
    for ids in member_ids_by_item.values():
        all_entity_ids.update(ids)
    state.entities = _load_entities(client, org_id, plan["source_job_id"], sorted(all_entity_ids))
    state.summaries = _load_summaries(client, org_id, plan["source_job_id"], sorted(all_entity_ids))

    state.snapshot_dir = _download_and_unpack_snapshot(client, org_id, plan["source_job_id"])

    items_by_cluster_id = {item["cluster_id"]: item for item in items}
    terminal_unit_ids, resumed_symbol_maps, resumed_unit_files, resumed_unit_test_files, resumed_stubbed_units = (
        _load_resume_state(client, org_id, execution_job["id"], items_by_cluster_id)
    )
    state.terminal_unit_ids = terminal_unit_ids
    state.symbol_maps.update(resumed_symbol_maps)
    state.unit_files.update(resumed_unit_files)
    state.unit_test_files.update(resumed_unit_test_files)
    state.stubbed_units.update(resumed_stubbed_units)
