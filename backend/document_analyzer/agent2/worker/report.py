"""Stage `report`: cluster reports — design_agent2.md §6.

Consumes the in-memory `CanonicalGraph` (§3) and `ClusterResult` (§4) and
writes one `cluster_reports` row per cluster, plus `clusters.label`:

1. **Units** (§6.1). One LLM call per unit, fanned out through a bounded
   thread pool (`AGENT2_REPORT_CONCURRENCY`, same pattern as Agent 1's
   summarize_fanout.py). The payload holds only the members' `entity_type`,
   `name`, `file_path` and entity-level `summary_text` (capped: `max_unit_size`
   members, or the 50 highest-degree for the foundation, plus an omitted
   count) and the unit's external call names. Never `raw_facts`, never
   another unit's members.
2. **Phases** (§6.2). One call per phase, through the same pool once every
   unit is done, over its units' labels and `summary_text` (capped at
   `PHASE_REPORT_UNIT_CAP` units).

Risk is never the LLM's call: `risk_level`/`risk_factors` come from Agent 1's
`rollup_risk` over the members' entity risk (units) or the units' report risk
(phases), and any risk the LLM returns is ignored. Grounding goes through
`check_grounding_with_retry` with the exact payload sent; an exhausted cap
stores `unverified` and never fails the job. Embeddings come from the pinned
`embed_text`. A response that still isn't `{label, summary_text}` JSON after
`REPORT_PARSE_RETRIES` extra attempts raises `ReportGenerationError`, which
fails the planning job with that message.

The LLM calls run in worker threads; every Supabase read and write happens on
the calling thread. Reruns delete this job's reports first (idempotent).
"""

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from openai import AzureOpenAI
from supabase import Client

from document_analyzer.agent2.worker.cluster import ClusterResult, Unit
from document_analyzer.agent2.worker.graph import CanonicalGraph, _fetch_all
from document_analyzer.supabase_client import insert_many_scoped
from document_analyzer.worker.embed import embed_text
from document_analyzer.worker.ground_retry import check_grounding_with_retry
from document_analyzer.worker.grounding_status import grounding_status_for
from document_analyzer.worker.risk_rollup import rollup_risk
from document_analyzer.worker.summarize import RiskLevel, get_azure_openai_client

# design_agent2.md §12 — only bounds throughput, never changes results, so it is
# read from env rather than frozen into planning_jobs.params.
REPORT_CONCURRENCY = int(os.environ.get("AGENT2_REPORT_CONCURRENCY", 10))

# §6.1: the foundation unit's payload is truncated to its highest-degree members.
FOUNDATION_MEMBER_CAP = 50
# §6.2: a phase payload carries at most this many units (largest first).
PHASE_REPORT_UNIT_CAP = 50
# Extra attempts at a malformed (non-JSON / missing field) report response.
REPORT_PARSE_RETRIES = 2

_UNIT_SYSTEM_PROMPT = """You are writing a report on one migration unit of a legacy PHP/WordPress \
codebase: a tightly-coupled group of functions, classes and hooks that will be migrated together. \
You are given ONLY the members of this one unit (type, name, file and an existing plain-English \
summary of each), how many members were left out of the list, and the names of external \
functions (PHP/WordPress builtins or libraries) the members call. Do not assume anything about \
the rest of the codebase.

Write:
- "label": a short title for the unit (at most 6 words), naming what it does.
- "summary_text": 2-4 plain-English sentences on what the unit does as a whole and what it \
depends on externally, synthesized from the member summaries.

Respond with a single JSON object and nothing else:
{"label": "...", "summary_text": "..."}
"""

_PHASE_SYSTEM_PROMPT = """You are writing a report on one migration phase of a legacy PHP/WordPress \
codebase: a group of migration units a reviewer will approve together. You are given ONLY the \
label and summary of each unit in this phase, and how many units were left out of the list. \
Do not assume anything about the rest of the codebase.

Write:
- "label": a short title for the phase (at most 6 words).
- "summary_text": 2-4 plain-English sentences on what this phase covers as a whole, \
synthesized from the unit summaries.

Respond with a single JSON object and nothing else:
{"label": "...", "summary_text": "..."}
"""


class ReportGenerationError(Exception):
    """The report LLM kept returning something other than `{label, summary_text}`."""


@dataclass(frozen=True)
class ClusterReport:
    """Matches ground_retry.SummaryLike; risk is the deterministic rollup, never the LLM's."""

    label: str
    summary_text: str
    risk_level: RiskLevel
    risk_factors: list[str]


@dataclass(frozen=True)
class _Outcome:
    cluster_id: str
    report: ClusterReport
    grounding_status: str


def _load_entity_summaries(client: Client, source_job_id: str, org_id: str) -> dict[str, dict]:
    rows = _fetch_all(
        lambda: client.table("knowledge_summaries")
        .select("id, entity_id, summary_text, risk_level")
        .eq("job_id", source_job_id)
        .eq("org_id", org_id)
        .eq("level", "entity")
    )
    return {row["entity_id"]: row for row in rows if row.get("entity_id")}


def _node_summary(graph: CanonicalGraph, summaries: dict[str, dict], node_id: str) -> dict | None:
    """The representative's summary, else any duplicate's (§3.2 prefers a summarized one)."""
    for entity_id in [node_id, *graph.members.get(node_id, [])]:
        if entity_id in summaries:
            return summaries[entity_id]
    return None


def _degrees(graph: CanonicalGraph) -> dict[str, int]:
    neighbours: dict[str, set[str]] = defaultdict(set)
    for a, b in graph.directed:
        neighbours[a].add(b)
        neighbours[b].add(a)
    return {n: len(ns) for n, ns in neighbours.items()}


def _qualified_name(node: dict) -> str:
    class_name = (node.get("raw_facts") or {}).get("class")
    return f"{class_name}::{node['name']}" if class_name else node["name"]


def external_call_names(graph: CanonicalGraph, node_ids: list[str]) -> list[str]:
    """Names in the members' `raw_facts.calls` that didn't resolve to a `calls` edge
    (builtins, WordPress core, libraries — php_extractor.py only emits resolved ones)."""
    resolved: dict[str, set[str]] = defaultdict(set)
    for (src, dst), counts in graph.directed.items():
        if counts.calls and dst in graph.nodes:
            callee = graph.nodes[dst]
            resolved[src].update({callee["name"], _qualified_name(callee)})
    names: set[str] = set()
    for node_id in node_ids:
        calls = (graph.nodes[node_id].get("raw_facts") or {}).get("calls") or []
        names.update(c for c in calls if isinstance(c, str) and c not in resolved[node_id])
    return sorted(names)


def unit_payload(
    graph: CanonicalGraph, unit: Unit, summaries: dict[str, dict], degrees: dict[str, int], max_unit_size: int
) -> dict:
    """§6.1 facts: the only thing the unit report call (and its grounding check) sees."""
    cap = FOUNDATION_MEMBER_CAP if unit.kind == "foundation" else max_unit_size
    ranked = sorted(unit.nodes, key=lambda n: (-degrees.get(n, 0), n))
    shown = ranked[:cap]
    members = []
    for node_id in shown:
        node = graph.nodes[node_id]
        summary = _node_summary(graph, summaries, node_id)
        members.append(
            {
                "entity_type": node["entity_type"],
                "name": node["name"],
                "file_path": node["file_path"],
                "summary_text": summary["summary_text"] if summary else None,
            }
        )
    return {
        "members": members,
        "omitted_member_count": len(ranked) - len(shown),
        "external_calls": external_call_names(graph, shown)[:cap],
    }


def phase_payload(units: list[tuple[Unit, ClusterReport]]) -> dict:
    """§6.2 facts: the phase's units' labels and summaries, largest units first."""
    ranked = sorted(units, key=lambda ur: (-len(ur[0].nodes), ur[0].nodes[0] if ur[0].nodes else ""))
    shown = ranked[:PHASE_REPORT_UNIT_CAP]
    return {
        "units": [{"label": r.label, "summary_text": r.summary_text} for _, r in shown],
        "omitted_unit_count": len(ranked) - len(shown),
    }


def _unit_risk(graph: CanonicalGraph, unit: Unit, summaries: dict[str, dict]) -> tuple[RiskLevel, list[str]]:
    named = []
    for node_id in unit.nodes:
        summary = _node_summary(graph, summaries, node_id)
        if summary and summary.get("risk_level"):
            named.append((_qualified_name(graph.nodes[node_id]), summary["risk_level"]))
    return rollup_risk(named)


def _generate(
    system_prompt: str,
    facts: dict,
    risk: tuple[RiskLevel, list[str]],
    llm_client: AzureOpenAI,
    what: str,
) -> ClusterReport:
    """One report attempt; malformed responses are retried up to REPORT_PARSE_RETRIES times."""
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    problem = ""
    for _ in range(REPORT_PARSE_RETRIES + 1):
        response = llm_client.chat.completions.create(
            model=deployment,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(facts)}],
        )
        content = response.choices[0].message.content
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            problem = f"not valid JSON: {content!r}"
            continue
        label = parsed.get("label") if isinstance(parsed, dict) else None
        summary_text = parsed.get("summary_text") if isinstance(parsed, dict) else None
        if not (isinstance(label, str) and label.strip() and isinstance(summary_text, str) and summary_text.strip()):
            problem = f"missing a non-empty 'label'/'summary_text': {parsed!r}"
            continue
        # Any risk the LLM volunteered is dropped: risk is the deterministic rollup (§6.3).
        return ClusterReport(
            label=label.strip()[:200], summary_text=summary_text.strip(), risk_level=risk[0], risk_factors=risk[1]
        )
    raise ReportGenerationError(
        f"report for {what}: LLM response still malformed after {REPORT_PARSE_RETRIES + 1} attempts, {problem}"
    )


def _report_with_grounding(
    cluster_id: str, system_prompt: str, facts: dict, risk, llm_client: AzureOpenAI, what: str
) -> _Outcome:
    retry = check_grounding_with_retry(
        facts, generate=lambda: _generate(system_prompt, facts, risk, llm_client, what), client=llm_client
    )
    return _Outcome(cluster_id=cluster_id, report=retry.summary, grounding_status=grounding_status_for(retry))


def build_reports(
    graph: CanonicalGraph,
    result: ClusterResult,
    summaries: dict[str, dict],
    params: dict,
    llm_client: AzureOpenAI,
    max_workers: int | None = None,
) -> list[_Outcome]:
    """Unit reports, then phase reports, through the bounded pool. No DB access."""
    degrees = _degrees(graph)
    max_unit_size = int(params["max_unit_size"])

    def unit_job(unit: Unit) -> _Outcome:
        facts = unit_payload(graph, unit, summaries, degrees, max_unit_size)
        risk = _unit_risk(graph, unit, summaries)
        return _report_with_grounding(unit.id, _UNIT_SYSTEM_PROMPT, facts, risk, llm_client, f"unit {unit.id}")

    unit_by_id = {u.id: u for u in result.units}

    def phase_job(phase) -> _Outcome:
        members = [(unit_by_id[uid], report_by_unit[uid]) for uid in phase.unit_ids]
        facts = phase_payload(members)
        risk = rollup_risk([(r.label, r.risk_level) for _, r in members])
        return _report_with_grounding(phase.id, _PHASE_SYSTEM_PROMPT, facts, risk, llm_client, f"phase {phase.id}")

    # Phases only depend on their own units' reports, so once every unit is done they
    # fan out through the same bounded pool.
    cap = max_workers if max_workers is not None else REPORT_CONCURRENCY
    with ThreadPoolExecutor(max_workers=cap) as executor:
        unit_outcomes = list(executor.map(unit_job, result.units))
        report_by_unit = {o.cluster_id: o.report for o in unit_outcomes}
        phase_outcomes = list(executor.map(phase_job, result.phases))
    return unit_outcomes + phase_outcomes


def persist_reports(
    client: Client, planning_job: dict, result: ClusterResult, outcomes: list[_Outcome], embed: Callable[[str], list[float]]
) -> None:
    org_id = planning_job["org_id"]
    cluster_ids = [u.id for u in result.units] + [p.id for p in result.phases]
    if cluster_ids:
        # Idempotent re-run over the same clusters.
        client.table("cluster_reports").delete().in_("cluster_id", cluster_ids).eq("org_id", org_id).execute()

    rows = [
        {
            "cluster_id": o.cluster_id,
            "summary_text": o.report.summary_text,
            "risk_level": o.report.risk_level,
            "risk_factors": o.report.risk_factors,
            "embedding": embed(o.report.summary_text),
            "grounding_status": o.grounding_status,
        }
        for o in outcomes
    ]
    if rows:
        insert_many_scoped(client, "cluster_reports", org_id, rows)
    for o in outcomes:
        client.table("clusters").update({"label": o.report.label}).eq("id", o.cluster_id).eq("org_id", org_id).execute()


def run_report_stage(
    client: Client,
    planning_job: dict,
    graph: CanonicalGraph,
    result: ClusterResult,
    llm_client: AzureOpenAI | None = None,
    embed: Callable[[str], list[float]] = embed_text,
    max_workers: int | None = None,
) -> list[_Outcome]:
    """Loads the source job's entity summaries, reports every unit then every phase,
    and persists `cluster_reports` + `clusters.label`."""
    summaries = _load_entity_summaries(client, planning_job["source_job_id"], planning_job["org_id"])
    outcomes = build_reports(
        graph, result, summaries, planning_job["params"], llm_client or get_azure_openai_client(), max_workers
    )
    persist_reports(client, planning_job, result, outcomes, embed)
    return outcomes
