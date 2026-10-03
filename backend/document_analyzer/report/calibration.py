"""Assessment Report calibration (design_report.md §7 step 2, specs_report.md §6.1).

The only DB-reading piece of the estimate. It reads the pooled, cross-org set of
completed Agent 3 units with the service role (decided 2026-10-01) and returns
**aggregates only**: per-bucket empirical values (cost, attempts, duration,
verdict). No org id, project id, path, cluster id or job id leaves this module.
Pooling across orgs is the point, so the queries are deliberately not filtered
by `org_id`; that's why nothing identifying may be returned.

Duration comes from `unit_executions.started_at/finished_at`. A unit without
both has `duration_s = None` (the estimator then uses the config default for
that sample) and is counted in `duration_missing`, which marks the calibration
lower-confidence for runtime.
"""

import hashlib
import json
from datetime import datetime

from supabase import Client

from document_analyzer.report.estimator import build_calibration, size_label

TERMINAL_STATUSES = ("passed", "failed_verification", "error")

_PAGE_SIZE = 1000
_IN_CHUNK = 200


def _pages(query_factory) -> list[dict]:
    rows, start = [], 0
    while True:
        page = query_factory().range(start, start + _PAGE_SIZE - 1).execute().data or []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def _in_chunks(client: Client, table: str, columns: str, key: str, values: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(values), _IN_CHUNK):
        chunk = values[i : i + _IN_CHUNK]
        out.extend(_pages(lambda: client.table(table).select(columns).in_(key, chunk).order(key)))
    return out


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _duration_s(unit: dict) -> float | None:
    started, finished = _parse(unit.get("started_at")), _parse(unit.get("finished_at"))
    if started is None or finished is None or finished < started:
        return None
    return (finished - started).total_seconds()


def aggregate_units(units: list[dict], sizes: dict, risks: dict, costs: dict, config: dict) -> dict:
    """Pure join step. `units`: terminal `unit_executions` rows; `sizes`: cluster_id ->
    `clusters.size`; `risks`: cluster_id -> `risk_level`; `costs`: (execution_job_id,
    cluster_id) -> `llm_cost_by_execution_unit` row. A unit with no cost row, or with
    unpriced calls, is unpriced (`cost_usd = None`) and is dropped and counted by
    `build_calibration`. A unit with no cluster report is skipped (no risk bucket).
    Returns `{"calibration", "snapshot"}`; ids are used for joining only."""
    unknown_risk = config.get("buckets", {}).get("unknown_risk", "medium")
    rows, duration_missing, dates = [], 0, []
    for unit in units:
        cost = costs.get((unit["execution_job_id"], unit["cluster_id"]))
        priced = cost is not None and not cost.get("unpriced_calls")
        duration = _duration_s(unit)
        duration_missing += duration is None
        finished = _parse(unit.get("finished_at")) or _parse(unit.get("updated_at"))
        if finished:
            dates.append(finished.date().isoformat())
        rows.append(
            {
                "size": size_label(sizes.get(unit["cluster_id"]), config)[0],
                "risk": risks.get(unit["cluster_id"], unknown_risk),
                "cost_usd": cost["cost_usd"] if priced else None,
                "attempts": unit.get("attempts"),
                "duration_s": duration,
                "verdict": unit["status"],
            }
        )
    calibration = build_calibration(rows)
    # Content hash of the aggregates: the same pooled history gives the same id.
    digest = hashlib.sha256(json.dumps(calibration, sort_keys=True).encode()).hexdigest()[:16]
    snapshot = {
        "id": digest,
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
        "units": len(rows),
        "duration_missing": duration_missing,
    }
    return {"calibration": calibration, "snapshot": snapshot}


def load_calibration(client: Client, config: dict) -> dict:
    """Read the pooled history with the service-role `client` and aggregate it.
    Read-only. An empty history is not an error: it yields empty buckets (cold start)."""
    units = _pages(
        lambda: client.table("unit_executions")
        .select("id, execution_job_id, cluster_id, status, attempts, started_at, finished_at, updated_at")
        .in_("status", list(TERMINAL_STATUSES))
        .order("id")
    )
    cluster_ids = sorted({u["cluster_id"] for u in units})
    sizes = {c["id"]: c["size"] for c in _in_chunks(client, "clusters", "id, size", "id", cluster_ids)}
    risks = {
        r["cluster_id"]: r["risk_level"]
        for r in _in_chunks(client, "cluster_reports", "cluster_id, risk_level", "cluster_id", cluster_ids)
    }
    job_ids = sorted({u["execution_job_id"] for u in units})
    costs = {
        (c["job_id"], c["cluster_id"]): c
        for c in _in_chunks(
            client, "llm_cost_by_execution_unit", "job_id, cluster_id, cost_usd, unpriced_calls", "job_id", job_ids
        )
    }
    return aggregate_units(units, sizes, risks, costs, config)
