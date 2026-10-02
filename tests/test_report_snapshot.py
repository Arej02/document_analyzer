"""report_json snapshot over a fixture project (tasks_report.md Phase 3).

`tests/fixtures/report_project/project.json` holds the rows of a small PHP shop (entities,
edges with Graphify provenance, summaries at every level, filtered files, secret findings,
coverage, an approved plan) and is loaded into the fake client. The goldens are the
canonical `report_json` minus `generated_at`. To regenerate after an intended change:

    UPDATE_GOLDEN=1 uv run python -m pytest tests/test_report_snapshot.py
"""

import json
import os
from pathlib import Path

import pytest

from document_analyzer.report.build import SCHEMA_VERSION, build_report_json, canonical_json, plan_units
from document_analyzer.report.collect import collect_plan
from document_analyzer.report.estimator import build_calibration, simulate
from tests.fakes import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "report_project"
ORG, JOB, PLAN = "org-1", "job-1", "plan-1"

CONFIG = {
    "buckets": {"size_edges": [3, 8, 20], "unknown_risk": "medium"},
    "defaults": {"fallback": {"cost_usd": 4.0, "attempts": 2, "duration_s": 120.0, "failure_rate": 0.25}},
    "calibration": {"min_samples": 5, "unpriced_threshold": 0.2},
    "simulation": {"seed": 1, "runs": 300, "concurrency": 2},
    "rework": {"hours_per_failed_unit": {"low": 2, "medium": 4, "high": 8}},
    "quality": {"sparse_calls_per_function": 0.5},
}
# 6 completed "m|high" units (enough history for that one bucket), 1 unpriced.
CALIBRATION_ROWS = [
    {"size": "m", "risk": "high", "cost_usd": c, "attempts": a, "duration_s": d, "verdict": v}
    for c, a, d, v in [(3.0, 1, 100.0, "passed"), (5.0, 2, 200.0, "passed"), (4.0, 1, 150.0, "passed"),
                       (9.0, 3, 400.0, "failed_verification"), (6.0, 2, 250.0, "passed"), (2.0, 1, 90.0, "passed")]
] + [{"size": "m", "risk": "high", "cost_usd": None, "attempts": 1, "duration_s": 100.0, "verdict": "passed"}]
SNAPSHOT = {"id": "snap-fixture", "date_from": "2026-09-01", "date_to": "2026-09-30", "units": 7, "duration_missing": 0}


def _client(reverse: bool = False) -> FakeClient:
    client = FakeClient(ORG)
    for table, rows in json.loads((FIXTURE / "project.json").read_text(encoding="utf-8")).items():
        client.table(table).rows.extend(reversed(rows) if reverse else rows)
    return client


def _build(client, tier, **overrides) -> dict:
    args = dict(
        job_id=JOB,
        tier=tier,
        project_name="Fixture Shop",
        snapshot_date="2026-10-01",
        generated_at="2026-10-01T12:00:00Z",
        template_version="1",
        estimator_version="1",
        estimator_config_hash="fixture-hash",
        next_steps={"heading": "Next steps", "body": "Talk to us.", "contact": "hello@example.com"},
        config=CONFIG,
        calibration_snapshot=SNAPSHOT,
        calibration_snapshot_id=SNAPSHOT["id"],
    )
    if tier == "full":
        plan, _ = collect_plan(client, ORG, JOB, PLAN)
        estimate = simulate(plan_units(plan), build_calibration(CALIBRATION_ROWS), CONFIG)
        args.update(plan_id=PLAN, estimate=estimate["customer"])
    args.update(overrides)
    return build_report_json(client, ORG, **args)


def _deterministic(report: dict) -> str:
    """Pretty canonical JSON of everything that is part of the byte-equality contract."""
    body = {k: v for k, v in report.items() if k != "generated_at"}
    return json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _assert_golden(name: str, actual: str) -> None:
    path = FIXTURE / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual, f"{name} differs; rerun with UPDATE_GOLDEN=1 if intended"


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_report_json_matches_golden(tier):
    _assert_golden(f"{tier}.golden.json", _deterministic(_build(_client(), tier)))


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_two_builds_with_the_same_inputs_are_byte_equal(tier):
    first, second = _build(_client(), tier), _build(_client(), tier, generated_at="2099-01-01T00:00:00Z")
    assert first["inputs"] == second["inputs"]
    assert _deterministic(first) == _deterministic(second)
    assert canonical_json({k: v for k, v in first.items() if k != "generated_at"}) == canonical_json(
        {k: v for k, v in second.items() if k != "generated_at"}
    )


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_row_order_in_the_database_does_not_change_the_output(tier):
    assert _deterministic(_build(_client(), tier)) == _deterministic(_build(_client(reverse=True), tier))


def test_tier_shapes_and_schema_version():
    xray, full = _build(_client(), "xray"), _build(_client(), "full")
    assert xray["schema_version"] == full["schema_version"] == SCHEMA_VERSION
    assert "plan" not in xray and "estimate" not in xray
    assert full["plan"]["status"] == "approved" and full["inputs"]["plan_version"] == 3
    assert full["cover"]["watermark"] is None
    assert full["estimate"]["basis"] == "mixed" and full["estimate"]["units_from_history"] == 1


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_no_secret_raw_fact_or_internal_cost_in_the_report(tier):
    text = canonical_json(_build(_client(), tier))
    assert "AKIAFAKEKEYVALUE123" not in text
    assert "[REDACTED" not in text and "internal only" not in text
    assert "cost" not in text.lower()
