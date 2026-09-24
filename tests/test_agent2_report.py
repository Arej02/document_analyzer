"""Tests for stage `report` (agent2/worker/report.py) — design_agent2.md §6.

The graph and clusters come from the real stage `graph`/`cluster` builders, and
one fake Azure OpenAI client answers both the report calls and the grounding
checks (told apart by system prompt), as the worker shares one client.
"""

import json
import threading
import time

import pytest

from document_analyzer.agent2.worker import report as report_mod
from document_analyzer.agent2.worker.cluster import Unit, build_clusters
from document_analyzer.agent2.worker.graph import EdgeCounts, build_graph
from document_analyzer.agent2.worker.report import (
    ReportGenerationError,
    build_reports,
    run_report_stage,
    unit_payload,
)

from document_analyzer.worker.ground_retry import GROUNDING_RETRY_CAP

from fakes import FakeClient

ORG = "org-1"
PARAMS = {"seed": 42, "alpha": 0.0, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}
PLANNING_JOB = {"id": "pj-1", "org_id": ORG, "source_job_id": "job-1", "params": PARAMS}


@pytest.fixture(autouse=True)
def azure_openai_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """`report(payload) -> content` answers report calls; grounding calls answer `grounded`."""

    def __init__(self, report=None, grounded=True, hold_seconds=0.0):
        self._report = report or (lambda payload: json.dumps({"label": "A unit", "summary_text": "Does things."}))
        self._grounded = grounded
        self._hold = hold_seconds
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0
        self.report_payloads: list[dict] = []
        self.grounding_calls = 0
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        system, user = kwargs["messages"][0]["content"], kwargs["messages"][1]["content"]
        if "fact-checking" in system:
            with self._lock:
                self.grounding_calls += 1
            return _Resp(json.dumps({"grounded": self._grounded, "issues": [] if self._grounded else ["x"]}))
        payload = json.loads(user)
        with self._lock:
            self.report_payloads.append(payload)
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        time.sleep(self._hold)
        with self._lock:
            self._in_flight -= 1
        return _Resp(self._report(payload))


def _entity(entity_id, file_path, name=None, calls=None, raw_facts_extra=None):
    return {
        "id": entity_id,
        "entity_type": "function",
        "name": name or entity_id,
        "file_path": file_path,
        "raw_facts": {"params": [], "calls": calls or [], "secret_marker": "RAW", **(raw_facts_extra or {})},
    }


def _edge(src, dst):
    return {"from_entity_id": src, "to_entity_id": dst, "edge_type": "calls"}


def _two_cliques(size=4):
    """Two cliques in separate files joined by one weak edge. a00 calls one builtin."""
    entities, edges = [], []
    for prefix, path in (("a", "a.php"), ("b", "b.php")):
        ids = [f"{prefix}{i:02d}" for i in range(size)]
        for i in ids:
            calls = [f"{prefix}01", "wp_remote_get"] if i == f"{prefix}00" else []
            entities.append(_entity(i, path, calls=calls))
        edges += [_edge(x, y) for n, x in enumerate(ids) for y in ids[n + 1 :]]
    edges.append(_edge("a00", "b01"))
    return entities, edges


def _setup(risk_by_id=None, size=4):
    entities, edges = _two_cliques(size)
    graph = build_graph(entities, {}, edges, alpha=0.0, hub_percentile=99)
    result = build_clusters(graph, PARAMS)
    risk_by_id = risk_by_id or {}
    summaries = {
        e["id"]: {"entity_id": e["id"], "summary_text": f"Summary of {e['id']}.", "risk_level": risk_by_id.get(e["id"], "low")}
        for e in entities
    }
    return graph, result, summaries


def _unit_of(result, node_id):
    return next(u for u in result.units if node_id in u.nodes)


def test_unit_payload_has_no_raw_facts_or_other_units_members():
    graph, result, summaries = _setup()
    llm = FakeLLM()
    build_reports(graph, result, summaries, PARAMS, llm)

    unit_a = _unit_of(result, "a00")
    other_names = {n for u in result.units if u.id != unit_a.id for n in u.nodes}
    payload = next(p for p in llm.report_payloads if "members" in p and {m["name"] for m in p["members"]} >= {"a00"})

    assert {m["name"] for m in payload["members"]} == set(unit_a.nodes)
    assert not ({m["name"] for m in payload["members"]} & other_names)
    for member in payload["members"]:
        assert set(member) == {"entity_type", "name", "file_path", "summary_text"}
    dumped = json.dumps(payload)
    assert "raw_facts" not in dumped and "RAW" not in dumped
    # b01 is resolved (an edge) and in another unit; only the builtin is external.
    assert payload["external_calls"] == ["wp_remote_get"]
    assert "b01" not in dumped


def test_llm_supplied_risk_is_ignored():
    graph, result, summaries = _setup(risk_by_id={"a02": "high"})
    llm = FakeLLM(report=lambda p: json.dumps({"label": "L", "summary_text": "S.", "risk_level": "low", "risk_factors": []}))
    outcomes = {o.cluster_id: o for o in build_reports(graph, result, summaries, PARAMS, llm)}

    unit_a = _unit_of(result, "a02")
    assert outcomes[unit_a.id].report.risk_level == "high"
    assert outcomes[unit_a.id].report.risk_factors == ["a02"]
    phase_a = next(p for p in result.phases if unit_a.id in p.unit_ids)
    assert outcomes[phase_a.id].report.risk_level == "high"


def test_exhausted_grounding_retries_store_unverified_and_continue():
    graph, result, summaries = _setup()
    llm = FakeLLM(grounded=False)
    client = FakeClient()
    outcomes = run_report_stage(client, PLANNING_JOB, graph, result, llm_client=llm, embed=lambda t: [0.0] * 384)

    clusters = len(result.units) + len(result.phases)
    assert len(outcomes) == clusters
    rows = client.table("cluster_reports").rows
    assert len(rows) == clusters
    assert {r["grounding_status"] for r in rows} == {"unverified"}
    assert llm.grounding_calls == clusters * (GROUNDING_RETRY_CAP + 1)


def test_grounded_reports_are_verified_and_persisted():
    graph, result, summaries = _setup()
    client = FakeClient()
    client.table("clusters").rows.extend({"id": u.id, "org_id": ORG} for u in result.units)
    client.table("clusters").rows.extend({"id": p.id, "org_id": ORG} for p in result.phases)
    run_report_stage(client, PLANNING_JOB, graph, result, llm_client=FakeLLM(), embed=lambda t: [0.5] * 384)

    rows = client.table("cluster_reports").rows
    assert {r["grounding_status"] for r in rows} == {"verified"}
    assert all(r["org_id"] == ORG and r["embedding"] == [0.5] * 384 for r in rows)
    assert {r["label"] for r in client.table("clusters").rows} == {"A unit"}


def test_rerun_leaves_one_report_per_cluster():
    graph, result, summaries = _setup()
    client = FakeClient()
    for _ in range(2):
        run_report_stage(client, PLANNING_JOB, graph, result, llm_client=FakeLLM(), embed=lambda t: [0.0])
    ids = [r["cluster_id"] for r in client.table("cluster_reports").rows]
    assert len(ids) == len(set(ids)) == len(result.units) + len(result.phases)


def test_phase_payload_holds_only_unit_labels_and_summaries():
    graph, result, summaries = _setup()
    llm = FakeLLM()
    build_reports(graph, result, summaries, PARAMS, llm)
    phase_payloads = [p for p in llm.report_payloads if "units" in p]
    assert len(phase_payloads) == len(result.phases)
    for payload in phase_payloads:
        assert all(set(u) == {"label", "summary_text"} for u in payload["units"])


def test_phase_reports_run_after_all_unit_reports():
    graph, result, summaries = _setup()
    llm = FakeLLM()
    build_reports(graph, result, summaries, PARAMS, llm)
    kinds = ["unit" if "members" in p else "phase" for p in llm.report_payloads]
    assert kinds == ["unit"] * len(result.units) + ["phase"] * len(result.phases)


def test_concurrency_cap_is_respected():
    entities, edges = [], []
    for c in range(8):  # 8 disconnected cliques -> 8 units
        ids = [f"c{c}_{i}" for i in range(3)]
        entities += [_entity(i, f"f{c}.php") for i in ids]
        edges += [_edge(x, y) for n, x in enumerate(ids) for y in ids[n + 1 :]]
    graph = build_graph(entities, {}, edges, alpha=0.0, hub_percentile=99)
    result = build_clusters(graph, PARAMS)
    assert len(result.units) == 8

    llm = FakeLLM(hold_seconds=0.05)
    build_reports(graph, result, {}, PARAMS, llm, max_workers=3)
    assert 1 < llm.peak_in_flight <= 3


def test_malformed_response_after_retries_fails_with_clear_error():
    graph, result, summaries = _setup()
    llm = FakeLLM(report=lambda p: "not json")
    with pytest.raises(ReportGenerationError, match=r"still malformed after 3 attempts.*not valid JSON"):
        build_reports(graph, result, summaries, PARAMS, llm)


def test_malformed_response_is_retried_before_failing():
    graph, result, summaries = _setup()
    attempts = {"n": 0}

    def flaky(payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return json.dumps({"label": "", "summary_text": "S."})
        return json.dumps({"label": "L", "summary_text": "S."})

    outcomes = build_reports(graph, result, summaries, PARAMS, FakeLLM(report=flaky), max_workers=1)
    assert all(o.report.label == "L" for o in outcomes)


def test_foundation_payload_is_truncated_to_highest_degree_members():
    entities = [_entity(f"n{i:03d}", "big.php") for i in range(60)]
    graph = build_graph(entities, {}, [], alpha=0.0, hub_percentile=99)
    # n059 has the most neighbours, so it must survive the cut.
    graph.directed = {("n059", f"n{i:03d}"): EdgeCounts(calls=1) for i in range(5)}
    unit = Unit(id="f", kind="foundation", nodes=sorted(graph.nodes))
    payload = unit_payload(graph, unit, {}, report_mod._degrees(graph), max_unit_size=25)

    assert len(payload["members"]) == report_mod.FOUNDATION_MEMBER_CAP
    assert payload["omitted_member_count"] == 10
    assert payload["members"][0]["name"] == "n059"
