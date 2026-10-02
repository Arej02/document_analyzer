"""Cost breakdowns for the operator-only cost endpoints (design.md section 7.4).

Reads `llm_cost_by_*` views and `llm_invocations` through the **service-role**
client. RLS does not apply to it, so every query here filters by both `org_id` (the
caller's, resolved server-side) and the job id; callers must also have confirmed the
job belongs to that org. Nothing here is reachable except through
`shared/cost_access.py::get_cost_operator`.

`cost_usd` is a list-price estimate, not the Azure invoice. Wherever any contributing
successful call had an unknown price, `cost_usd` is `null` (and `unpriced_calls` says
how many) rather than a silently low sum.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from supabase import Client

_PAGE = 1000  # PostgREST's default row cap


def _num(value: Any) -> float:
    return float(Decimal(str(value))) if value is not None else 0.0


def _stats(row: dict) -> dict:
    unpriced = int(row.get("unpriced_calls") or 0)
    return {
        "calls": int(row.get("calls") or 0),
        "error_calls": int(row.get("error_calls") or 0),
        "unpriced_calls": unpriced,
        "tokens": int(row.get("tokens") or 0),
        "cost_usd": None if unpriced else _num(row.get("cost_usd")),
    }


def _view_rows(client: Client, view: str, org_id: str, job_type: str, job_id: str, order: str | None) -> list[dict]:
    query = client.table(view).select("*").eq("org_id", org_id).eq("job_type", job_type).eq("job_id", job_id)
    if order:
        query = query.order(order)
    return query.execute().data or []


def build_job_cost_report(client: Client, org_id: str, job_type: str, job_id: str) -> dict:
    """Totals plus by-stage and by-purpose for one ingestion / planning / execution job."""
    total_rows = _view_rows(client, "llm_cost_by_job", org_id, job_type, job_id, None)
    return {
        "total": _stats(total_rows[0] if total_rows else {}),
        "by_stage": [
            {"stage": r["stage"], **_stats(r)}
            for r in _view_rows(client, "llm_cost_by_job_stage", org_id, job_type, job_id, "stage")
        ],
        "by_purpose": [
            {"purpose": r["purpose"], **_stats(r)}
            for r in _view_rows(client, "llm_cost_by_job_purpose", org_id, job_type, job_id, "purpose")
        ],
    }


def build_execution_unit_costs(client: Client, org_id: str, job_id: str) -> list[dict]:
    """Agent 3: one row per unit. `rounds` is the number of outer sandbox-feedback rounds
    that made LLM calls and `attempts` the highest inner attempt number reached."""
    rows = (
        client.table("llm_cost_by_execution_unit")
        .select("*")
        .eq("org_id", org_id)
        .eq("job_id", job_id)
        .order("cluster_id")
        .execute()
        .data
        or []
    )
    return [
        {"cluster_id": r["cluster_id"], "rounds": int(r.get("rounds") or 0), "attempts": int(r.get("max_attempt") or 0), **_stats(r)}
        for r in rows
    ]


def build_qa_cost_report(client: Client, org_id: str, source_job_id: str) -> dict:
    """Q&A spend against one ingestion job. Q&A rows have no job id (a question is a
    request, not a job), so they are found by `source_job_id` and summed here."""
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("llm_invocations")
            .select("purpose, stage, status, cost_usd, total_tokens")
            .eq("org_id", org_id)
            .eq("job_type", "qa")
            .eq("source_job_id", source_job_id)
            .order("created_at")
            .range(start, start + _PAGE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < _PAGE:
            break
        start += _PAGE

    def aggregate(group: list[dict]) -> dict:
        return _stats(
            {
                "calls": len(group),
                "error_calls": sum(1 for r in group if r["status"] == "error"),
                "unpriced_calls": sum(1 for r in group if r["status"] == "ok" and r["cost_usd"] is None),
                "tokens": sum(r["total_tokens"] or 0 for r in group),
                "cost_usd": sum((Decimal(str(r["cost_usd"])) for r in group if r["cost_usd"] is not None), Decimal(0)),
            }
        )

    by_stage: dict[Any, list[dict]] = defaultdict(list)
    by_purpose: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        by_stage[r["stage"]].append(r)
        by_purpose[r["purpose"]].append(r)
    return {
        "total": aggregate(rows),
        "by_stage": [{"stage": k, **aggregate(v)} for k, v in sorted(by_stage.items(), key=lambda kv: str(kv[0]))],
        "by_purpose": [{"purpose": k, **aggregate(v)} for k, v in sorted(by_purpose.items())],
    }
