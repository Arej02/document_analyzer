"""report/limitations.py — generated §5.8 assumptions and limitations."""

from document_analyzer.report.build import build_report_json
from document_analyzer.report.collect import collect_quality
from document_analyzer.report.limitations import build_limitations
from tests.test_report_build import COMMON, ORG, JOB, client  # noqa: F401  (fixture)

QUALITY = {"functions": 10, "resolved_calls": 10, "graphify_edges": 0, "inferred_edges": 0, "summaries_by_level": {"entity": {"total": 10, "unverified": 0}}}
ESTIMATE = {
    "units": 4, "units_from_defaults": 3, "n": 25, "concurrency": 2,
    "rework_hours_per_failed_unit": {"low": 2, "high": 8},
    "buckets": [
        {"size": "xs", "risk": "low", "units": 1, "n": 25, "source": "historical"},
        {"size": "m", "risk": "high", "units": 3, "n": 4, "source": "defaults"},
    ],
    "limitations": ["1 unit(s) had no risk report and were assumed medium risk."],
}


def codes(items):
    return [i["code"] for i in items]


def test_clean_quality_gives_nothing():
    assert build_limitations(QUALITY, {}) == []


def test_unverified_share_by_level_with_percentages():
    quality = {**QUALITY, "summaries_by_level": {
        "module": {"total": 3, "unverified": 1},
        "entity": {"total": 8, "unverified": 2},
        "repo": {"total": 1, "unverified": 0},
    }}
    (item,) = build_limitations(quality, {})
    assert item["code"] == "unverified_summaries"
    assert "entity 25% (2 of 8)" in item["text"] and "module 33% (1 of 3)" in item["text"] and "repo" not in item["text"]


def test_sparse_call_graph_uses_configured_threshold():
    quality = {**QUALITY, "resolved_calls": 3}
    assert codes(build_limitations(quality, {})) == ["sparse_call_graph"]  # 0.3 < default 0.5
    assert build_limitations(quality, {"quality": {"sparse_calls_per_function": 0.2}}) == []
    assert build_limitations({**QUALITY, "functions": 0, "resolved_calls": 0}, {}) == []


def test_inferred_edge_share_only_when_graphify_edges_exist():
    quality = {**QUALITY, "graphify_edges": 8, "inferred_edges": 2}
    (item,) = build_limitations(quality, {})
    assert item["code"] == "inferred_edges" and "25%" in item["text"] and "2 of 8" in item["text"]
    assert build_limitations({**QUALITY, "graphify_edges": 8}, {}) == []


def test_cold_start_buckets_and_estimate_assumptions():
    items = build_limitations(QUALITY, {"calibration": {"min_samples": 20}}, estimate=ESTIMATE,
                              calibration_snapshot={"date_from": "2026-01-01", "date_to": "2026-02-01"})
    assert codes(items) == ["cold_start_buckets", "estimate_assumptions", "estimate_note"]
    assert "3 of 4" in items[0]["text"] and "m/high (n=4)" in items[0]["text"] and "xs/low" not in items[0]["text"]
    assert "25 past unit(s), 2026-01-01 to 2026-02-01" in items[1]["text"]
    assert "2 unit(s) run at a time" in items[1]["text"] and "high 8 h" in items[1]["text"]


def test_exec_summary_status_limitations():
    assert codes(build_limitations(QUALITY, {}, executive_summary={"status": "unverified"})) == ["executive_summary_unverified"]
    assert codes(build_limitations(QUALITY, {}, executive_summary={"status": "withheld"})) == ["executive_summary_withheld"]
    assert build_limitations(QUALITY, {}, executive_summary={"status": "verified"}) == []


def test_collect_quality_counts_from_the_database(client):  # noqa: F811
    client._tables["entity_edges"].rows[0]["provenance"] = {"confidence": "INFERRED"}
    client._tables["entity_edges"].rows[1]["provenance"] = {"confidence": "EXTRACTED"}
    client._tables["knowledge_summaries"].rows[1]["grounding_status"] = "unverified"
    quality = collect_quality(client, ORG, JOB)
    assert quality["functions"] == 2 and quality["resolved_calls"] == 2
    assert (quality["graphify_edges"], quality["inferred_edges"]) == (2, 1)
    assert quality["summaries_by_level"] == {"entity": {"total": 3, "unverified": 1}, "repo": {"total": 1, "unverified": 0}}


def test_build_adds_generated_limitations_and_stays_deterministic(client):  # noqa: F811
    client._tables["knowledge_summaries"].rows[0]["grounding_status"] = "unverified"
    kwargs = dict(tier="xray", executive_summary={"text": "t", "status": "unverified", "note": "n"}, **COMMON)
    first = build_report_json(client, ORG, **kwargs)
    assert {"unverified_summaries", "executive_summary_unverified"} <= set(codes(first["limitations"]))
    assert first == build_report_json(client, ORG, **kwargs)
