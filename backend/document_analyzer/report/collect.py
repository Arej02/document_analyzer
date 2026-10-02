"""Assessment Report collectors (design_report.md §6, specs_report.md §4–5).

One read-only collector per section. Each returns `(data, limitations)`, where a
limitation is `{"code", "text"}` for §5.8. Rules, enforced by `_rows`:

- Explicit column lists only: never `*`, never `source_chunks.content`, never
  `raw_facts` values (`FORBIDDEN_COLUMNS`; a test asserts no query selects one).
- Service-role client; every query is filtered by `org_id` **and** the job id (or the
  plan / planning job id for plan tables).
- Output is explicitly ordered (sort keys, never DB order), so the same inputs give
  byte-equal deterministic sections.

Gap handled here, not hidden: Agent 1 stores module and subsystem summaries without a
name or a link to their module, so they can't be attributed. Subsystems and modules are
derived the way Agent 1 derives them (`subsystem_for_file`, one file = one module), and
their risk is rolled up from entity summaries with `rollup_risk`.
"""

import re
from collections import Counter, defaultdict

from supabase import Client

from document_analyzer.worker.risk_rollup import rollup_risk
from document_analyzer.worker.summarize_subsystem import subsystem_for_file

FORBIDDEN_COLUMNS = frozenset({"*", "content", "raw_facts", "embedding", "excerpt", "source_excerpt"})
ELIGIBLE_PLAN_STATUSES = ("draft", "in_review", "approved")
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

SAMPLE_CAP = 10  # coverage sample paths
HUBS_TOP_N = 10
CONNECTIVITY_TOP_N = 20
HOTSPOTS_PER_SUBSYSTEM = 5
LOCATIONS_PER_REASON = 50

_PAGE_SIZE = 1000
_IN_CHUNK = 200

# Best-effort keyword match on the stored `risk_factors` names (the rubric's own high-risk
# signals, design.md "Risk level heuristic"). Counts entities, never re-scans code.
# (whole-word tokens, raw substrings): tokens avoid "eval" matching "retrieval".
RISKY_PATTERNS = {
    "dynamic_code_execution": (("eval", "create_function"), ("call_user_func", "dynamic_exec")),
    "shell_or_process_call": (("shell", "exec", "system", "passthru", "proc_open", "popen"), ("shell_exec",)),
    "unserialize_on_input": (("unserialize",), ()),
    "sql_built_by_concatenation": (("sql",), ()),
    "superglobal_input": (("superglobal",), ("$_get", "$_post", "$_request", "$_cookie")),
}

SECURITY_CAVEAT = (
    "This is a static scan with known false negatives, not a penetration test. "
    "Findings are counts and locations only; no secret values are stored or shown."
)


class ReportInputError(Exception):
    """A report input is unusable (e.g. the pinned plan doesn't match the report's job)."""


def _limit(code: str, text: str) -> dict:
    return {"code": code, "text": text}


def _rows(
    client: Client, table: str, columns: str, order: str, filters: dict, in_: tuple[str, list] | None = None
) -> list[dict]:
    names = {c.strip() for c in columns.split(",")}
    if names & FORBIDDEN_COLUMNS:
        raise ValueError(f"{table}: forbidden column(s) {sorted(names & FORBIDDEN_COLUMNS)}")

    def pages(extra) -> list[dict]:
        out, start = [], 0
        while True:
            query = client.table(table).select(columns)
            for key, value in filters.items():
                query = query.eq(key, value)
            if extra:
                query = query.in_(*extra)
            page = query.order(order).range(start, start + _PAGE_SIZE - 1).execute().data or []
            out.extend(page)
            if len(page) < _PAGE_SIZE:
                return out
            start += _PAGE_SIZE

    if in_ is None:
        return pages(None)
    key, values = in_
    out: list[dict] = []
    for i in range(0, len(values), _IN_CHUNK):
        out.extend(pages((key, values[i : i + _IN_CHUNK])))
    return out


def _entities(client: Client, org_id: str, job_id: str) -> list[dict]:
    return _rows(client, "extracted_entities", "id, entity_type, name, file_path", "id", {"org_id": org_id, "job_id": job_id})


def _entity_summaries(client: Client, org_id: str, job_id: str) -> list[dict]:
    rows = _rows(
        client,
        "knowledge_summaries",
        "entity_id, level, summary_text, risk_level, risk_factors, grounding_status",
        "id",
        {"org_id": org_id, "job_id": job_id},
    )
    return rows


# --- glance (§5.2) -------------------------------------------------------------------


def _discovery(client: Client, org_id: str, job_id: str, job_row: dict) -> tuple[dict | None, list[dict]]:
    stats = job_row.get("discovery_stats")
    if stats:
        return stats, []
    events = (
        client.table("workflow_events")
        .select("data, created_at")
        .eq("org_id", org_id)
        .eq("workflow_kind", "agent1_ingestion")
        .eq("workflow_id", job_id)
        .eq("event_type", "stats.updated")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if events:
        return events[0]["data"], [
            _limit("stats_from_events", "Lines of code, languages and frameworks were read from the run's last progress event.")
        ]
    return None, [_limit("stats_not_recorded", "Lines of code, languages, frameworks and dependencies are not recorded for this run.")]


def _coverage(raw: dict | None) -> tuple[dict | None, list[dict]]:
    """Allow-list of `extraction_coverage`: statuses and counts, samples capped. The
    evidence report and any free-text reason are left out."""
    if not raw:
        return None, [_limit("coverage_not_recorded", "Parse coverage is not recorded for this run.")]
    graphify = raw.get("graphify") or {}
    sample = [
        {"path": item.get("path"), "status": item.get("status"), "reason": item.get("reason")}
        for item in (graphify.get("not_extracted_sample") or [])[:SAMPLE_CAP]
    ]
    coverage = {
        "graphify_status": graphify.get("status"),
        "counts_by_status": dict(sorted((graphify.get("counts_by_status") or {}).items())),
        "by_language": dict(sorted((graphify.get("by_language") or {}).items())),
        "not_extracted_count": graphify.get("not_extracted_count"),
        "not_extracted_sample": sample,
    }
    limitations = []
    if graphify.get("status") == "unavailable":
        limitations.append(_limit("graphify_unavailable", "Languages other than PHP were not parsed because the parser was unavailable."))
    if graphify.get("not_extracted_count"):
        limitations.append(_limit("files_not_parsed", f"{graphify['not_extracted_count']} file(s) could not be fully parsed."))
    return coverage, limitations


def collect_glance(client: Client, org_id: str, job_id: str) -> tuple[dict, list[dict]]:
    jobs = _rows(client, "ingestion_jobs", "id, discovery_stats, extraction_coverage", "id", {"org_id": org_id, "id": job_id})
    job = jobs[0] if jobs else {}
    stats, limitations = _discovery(client, org_id, job_id, job)
    stats = stats or {}

    languages = sorted(
        ({"name": k.split(".", 1)[1], "files": int(v)} for k, v in stats.items() if k.startswith("language.")),
        key=lambda item: (-item["files"], item["name"]),
    )
    frameworks = sorted(k.split(".", 1)[1] for k, v in stats.items() if k.startswith("framework.") and v)

    filtered = Counter(
        row["reason"] for row in _rows(client, "filtered_files", "id, reason", "id", {"org_id": org_id, "job_id": job_id})
    )
    entities = _entities(client, org_id, job_id)
    coverage, coverage_limits = _coverage(job.get("extraction_coverage"))

    data = {
        "loc": stats.get("loc"),
        "languages": languages,
        "frameworks": frameworks,
        "dependencies": stats.get("dependencies"),
        "files_analysed": len({e["file_path"] for e in entities}),
        "files_filtered": [{"reason": r, "count": n} for r, n in sorted(filtered.items(), key=lambda kv: (-kv[1], kv[0]))],
        "entities_by_type": dict(sorted(Counter(e["entity_type"] for e in entities).items())),
        "coverage": coverage,
    }
    return data, limitations + coverage_limits


# --- architecture (§5.3) ---------------------------------------------------------------


def _degrees(client: Client, org_id: str, job_id: str) -> tuple[Counter, list[dict]]:
    edges = _rows(client, "entity_edges", "id, from_entity_id, to_entity_id, edge_type", "id", {"org_id": org_id, "job_id": job_id})
    degree: Counter = Counter()
    for edge in edges:
        degree[edge["from_entity_id"]] += 1
        if edge["to_entity_id"]:
            degree[edge["to_entity_id"]] += 1
    return degree, edges


def _foundation_hubs(client: Client, org_id: str, plan: dict, entities_by_id: dict, degree: Counter) -> list[dict] | None:
    clusters = _rows(
        client, "clusters", "id, kind", "id", {"org_id": org_id, "planning_job_id": plan["planning_job_id"]}
    )
    foundation_ids = [c["id"] for c in clusters if c["kind"] == "foundation"]
    if not foundation_ids:
        return None
    members = _rows(
        client, "cluster_members", "cluster_id, entity_id", "entity_id", {"org_id": org_id}, in_=("cluster_id", foundation_ids)
    )
    hubs = [
        {"name": entities_by_id[m["entity_id"]]["name"], "file_path": entities_by_id[m["entity_id"]]["file_path"], "degree": degree[m["entity_id"]]}
        for m in members
        if m["entity_id"] in entities_by_id
    ]
    return hubs or None


def collect_architecture(client: Client, org_id: str, job_id: str, plan: dict | None = None) -> tuple[dict, list[dict]]:
    """`plan` is the `migration_plans` row of a Full Assessment (its `foundation` unit
    supplies the hubs); without one, hubs are the top-N entities by degree."""
    entities = _entities(client, org_id, job_id)
    by_id = {e["id"]: e for e in entities}
    summaries = {s["entity_id"]: s for s in _entity_summaries(client, org_id, job_id) if s["level"] == "entity"}
    degree, edges = _degrees(client, org_id, job_id)
    limitations = [
        _limit(
            "summaries_not_attributable",
            "Module and subsystem summaries are not linked to a named module in the stored data, so subsystems are "
            "listed with a risk level rolled up from their entities rather than a summary.",
        )
    ]

    by_subsystem: dict[str, list[dict]] = defaultdict(list)
    for entity in entities:
        by_subsystem[subsystem_for_file(entity["file_path"])].append(entity)
    subsystems = []
    for name, members in sorted(by_subsystem.items()):
        levels = [(e["name"], summaries[e["id"]]["risk_level"]) for e in members if e["id"] in summaries]
        subsystems.append(
            {
                "name": name,
                "summary": None,
                "summary_grounding": None,
                "risk_level": rollup_risk(levels)[0],
                "modules": sorted({e["file_path"] for e in members}),
            }
        )

    hubs = _foundation_hubs(client, org_id, plan, by_id, degree) if plan else None
    if hubs is None:
        hubs = [
            {"name": by_id[eid]["name"], "file_path": by_id[eid]["file_path"], "degree": d}
            for eid, d in degree.items()
            if eid in by_id
        ]
        if plan:
            limitations.append(_limit("hubs_by_degree", "The plan has no foundation unit, so hubs are the most-connected entities."))
    hubs = sorted(hubs, key=lambda h: (-h["degree"], h["name"], h["file_path"]))[:HUBS_TOP_N]

    pairs: Counter = Counter()
    for edge in edges:
        src, dst = by_id.get(edge["from_entity_id"]), by_id.get(edge["to_entity_id"])
        if src and dst:
            a, b = subsystem_for_file(src["file_path"]), subsystem_for_file(dst["file_path"])
            if a != b:
                pairs[(a, b)] += 1
    connectivity = [
        {"from": a, "to": b, "edges": n} for (a, b), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:CONNECTIVITY_TOP_N]
    ]
    return {"subsystems": subsystems, "hubs": hubs, "connectivity": connectivity}, limitations


# --- risk (§5.4) -----------------------------------------------------------------------


def collect_risk(client: Client, org_id: str, job_id: str) -> tuple[dict, list[dict]]:
    entities = {e["id"]: e for e in _entities(client, org_id, job_id)}
    summaries = [s for s in _entity_summaries(client, org_id, job_id) if s["level"] == "entity" and s["entity_id"] in entities]

    heat: dict[str, Counter] = defaultdict(Counter)
    grouped: dict[str, list[dict]] = defaultdict(list)
    signal_missing = 0
    for summary in summaries:
        entity = entities[summary["entity_id"]]
        subsystem = subsystem_for_file(entity["file_path"])
        heat[subsystem][summary["risk_level"]] += 1
        if summary["risk_level"] not in ("high", "medium"):
            continue
        factors = summary.get("risk_factors") or []
        signal_missing += not factors
        grouped[subsystem].append(
            {
                "name": entity["name"],
                "file_path": entity["file_path"],
                "risk_level": summary["risk_level"],
                "signal": ", ".join(str(f) for f in factors) if factors else None,
                "explanation": summary["summary_text"],
            }
        )

    hotspots = []
    for subsystem, items in sorted(grouped.items()):
        items.sort(key=lambda i: (RISK_ORDER[i["risk_level"]], i["file_path"], i["name"]))
        hotspots.append({"subsystem": subsystem, "entities": items[:HOTSPOTS_PER_SUBSYSTEM], "more": max(0, len(items) - HOTSPOTS_PER_SUBSYSTEM)})
    heat_map = [{"subsystem": s, "low": c["low"], "medium": c["medium"], "high": c["high"]} for s, c in sorted(heat.items())]

    limitations = []
    if signal_missing:
        limitations.append(
            _limit("signal_not_recorded", f"The risk signal was not recorded for {signal_missing} flagged entit(ies) of this run.")
        )
    return {"heat_map": heat_map, "hotspots": hotspots}, limitations


# --- security (§5.5) ---------------------------------------------------------------------


def collect_security(client: Client, org_id: str, job_id: str) -> tuple[dict, list[dict]]:
    entities = {e["id"]: e for e in _entities(client, org_id, job_id)}
    findings = _rows(client, "secret_findings", "id, entity_id, field_path, reason", "id", {"org_id": org_id, "job_id": job_id})
    by_reason: dict[str, set] = defaultdict(set)
    counts: Counter = Counter()
    for finding in findings:
        counts[finding["reason"]] += 1
        entity = entities.get(finding["entity_id"])
        by_reason[finding["reason"]].add((entity["file_path"] if entity else None, finding["field_path"]))
    secrets = [
        {
            "reason": reason,
            "count": counts[reason],
            "locations": [
                {"file_path": path, "field_path": field}
                for path, field in sorted(locs, key=lambda l: (l[0] or "", l[1]))[:LOCATIONS_PER_REASON]
            ],
        }
        for reason, locs in sorted(by_reason.items())
    ]

    patterns: Counter = Counter()
    for summary in _entity_summaries(client, org_id, job_id):
        if summary["level"] != "entity" or summary["risk_level"] != "high":
            continue
        text = " ".join(str(f) for f in (summary.get("risk_factors") or [])).lower()
        tokens = set(re.split(r"[^a-z0-9]+", text))
        for pattern, (words, substrings) in RISKY_PATTERNS.items():
            if tokens.intersection(words) or any(s in text for s in substrings):
                patterns[pattern] += 1
    risky = [{"pattern": p, "count": patterns[p]} for p in sorted(patterns)]
    return {"secrets_by_reason": secrets, "risky_patterns": risky, "caveat": SECURITY_CAVEAT}, []


# --- graph and summary quality (§5.8) ----------------------------------------------------


def collect_quality(client: Client, org_id: str, job_id: str) -> dict:
    """Counts behind the generated limitations: how well the call graph resolved and how many
    summaries are unverified. Facts only; `report/limitations.py` turns them into text."""
    functions = sum(e["entity_type"] == "function" for e in _entities(client, org_id, job_id))
    edges = _rows(client, "entity_edges", "id, to_entity_id, edge_type, provenance", "id", {"org_id": org_id, "job_id": job_id})
    resolved_calls = sum(e["edge_type"] == "calls" and e["to_entity_id"] is not None for e in edges)
    # Only Graphify records a confidence; PHP-family edges are directly parsed facts.
    graded = [(e.get("provenance") or {}).get("confidence") for e in edges]
    graded = [c for c in graded if c]
    levels: dict[str, dict] = {}
    for summary in _entity_summaries(client, org_id, job_id):
        counts = levels.setdefault(summary["level"], {"total": 0, "unverified": 0})
        counts["total"] += 1
        counts["unverified"] += summary["grounding_status"] != "verified"
    return {
        "functions": functions,
        "resolved_calls": resolved_calls,
        "graphify_edges": len(graded),
        "inferred_edges": sum(c == "INFERRED" for c in graded),
        "summaries_by_level": dict(sorted(levels.items())),
    }


# --- plan (§5.6, Full only) ----------------------------------------------------------------


def load_plan_row(client: Client, org_id: str, plan_id: str, job_id: str) -> dict:
    """The pinned plan, re-checked against the report's job (design_report.md §6)."""
    rows = _rows(
        client,
        "migration_plans",
        "id, planning_job_id, source_job_id, version, status, open_facade_flags",
        "id",
        {"org_id": org_id, "id": plan_id},
    )
    if not rows:
        raise ReportInputError("plan not found")
    plan = rows[0]
    if plan["source_job_id"] != job_id:
        raise ReportInputError("the plan is pinned to a different ingestion job than the report")
    if plan["status"] not in ELIGIBLE_PLAN_STATUSES:
        raise ReportInputError(f"a {plan['status']} plan can't be reported on")
    return plan


def collect_plan(client: Client, org_id: str, job_id: str, plan_id: str) -> tuple[dict, list[dict]]:
    plan = load_plan_row(client, org_id, plan_id, job_id)
    planning_job_id = plan["planning_job_id"]
    jobs = _rows(client, "planning_jobs", "id, params", "id", {"org_id": org_id, "id": planning_job_id})
    max_unit_size = ((jobs[0]["params"] if jobs else None) or {}).get("max_unit_size")

    items = _rows(client, "migration_plan_items", "cluster_id, phase_cluster_id, phase_order, unit_order", "cluster_id", {"org_id": org_id, "plan_id": plan_id})
    cluster_ids = sorted({i["cluster_id"] for i in items} | {i["phase_cluster_id"] for i in items})
    clusters = {
        c["id"]: c for c in _rows(client, "clusters", "id, kind, size, label", "id", {"org_id": org_id}, in_=("id", cluster_ids))
    }
    risks = {
        r["cluster_id"]: r
        for r in _rows(client, "cluster_reports", "cluster_id, risk_level, grounding_status", "cluster_id", {"org_id": org_id}, in_=("cluster_id", cluster_ids))
    }

    def label(cid: str) -> str:
        return (clusters.get(cid) or {}).get("label") or "(unlabelled)"

    phases: dict[int, dict] = {}
    for item in sorted(items, key=lambda i: (i["phase_order"], i["unit_order"], i["cluster_id"])):
        phase = phases.setdefault(item["phase_order"], {"order": item["phase_order"], "label": label(item["phase_cluster_id"]), "units": []})
        cluster = clusters.get(item["cluster_id"]) or {}
        report = risks.get(item["cluster_id"]) or {}
        phase["units"].append(
            {
                "label": label(item["cluster_id"]),
                "kind": cluster.get("kind"),
                "size": cluster.get("size"),
                "risk_level": report.get("risk_level"),
                "grounding_status": report.get("grounding_status"),
            }
        )

    edges = _rows(
        client, "cluster_edges", "id, level, from_cluster_id, to_cluster_id, flag, scc_id", "id", {"org_id": org_id, "planning_job_id": planning_job_id}
    )
    sccs: dict[int, set] = defaultdict(set)
    for edge in edges:
        if edge["level"] == "unit" and edge["flag"] == "needs_facade":
            sccs[edge["scc_id"] if edge["scc_id"] is not None else -1].update((label(edge["from_cluster_id"]), label(edge["to_cluster_id"])))
    cycles = sorted(({"units": sorted(units)} for units in sccs.values()), key=lambda c: c["units"])

    phase_order_of = {i["phase_cluster_id"]: i["phase_order"] for i in items}
    dependencies = sorted(
        {
            (phase_order_of[e["from_cluster_id"]], phase_order_of[e["to_cluster_id"]])
            for e in edges
            if e["level"] == "phase" and e["from_cluster_id"] in phase_order_of and e["to_cluster_id"] in phase_order_of
        }
    )
    unit_clusters = [clusters[i["cluster_id"]] for i in items if i["cluster_id"] in clusters]
    oversized = (
        sorted(
            ({"label": c.get("label") or "(unlabelled)", "size": c["size"]} for c in unit_clusters if c["size"] > max_unit_size),
            key=lambda o: (-o["size"], o["label"]),
        )
        if max_unit_size is not None
        else []
    )

    limitations = []
    if plan["status"] != "approved":
        limitations.append(_limit("plan_not_approved", f"The plan is {plan['status']}, not approved; this report is marked as a draft."))
    data = {
        "status": plan["status"],
        "version": plan["version"],
        "phases": [phases[k] for k in sorted(phases)],
        "phase_dependencies": [{"from": a, "to": b} for a, b in dependencies],
        "cycles_needing_facades": cycles,
        "oversized_units": oversized,
        "max_unit_size": max_unit_size,
        "units": len(unit_clusters),
    }
    return data, limitations
