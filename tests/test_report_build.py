"""report/build.py — design_report.md §5–6: tiers, plan checks, determinism."""

import pytest

from document_analyzer.report.build import SCHEMA_VERSION, build_report_json, canonical_json, plan_units
from document_analyzer.report.collect import ReportInputError, collect_plan
from tests.test_report_collect import JOB, ORG, PLAN, _plan_rows, client  # noqa: F401  (fixture)

COMMON = dict(
    job_id=JOB,
    project_name="Shop",
    snapshot_date="2026-10-01",
    generated_at="2026-10-01T00:00:00Z",
    template_version="1",
    estimator_version="1",
    estimator_config_hash="abc",
    next_steps={"heading": "Next", "body": "Talk to us.", "contact": "a@b.c"},
)
ESTIMATE = {
    "units": 3, "basis": "defaults", "n": 0, "units_from_history": 0, "units_from_defaults": 3, "concurrency": 1,
    "rework_hours_per_failed_unit": {"low": 2}, "limitations": [],
    "buckets": [{"size": "m", "risk": "low", "units": 3, "n": 0, "source": "defaults"}],
}


def _xray(client, **kw):
    return build_report_json(client, ORG, tier="xray", **COMMON, **kw)


def _full(client, **kw):
    return build_report_json(client, ORG, tier="full", plan_id=PLAN, estimate=ESTIMATE, **COMMON, **kw)


def test_xray_omits_plan_and_estimate_keys(client):
    report = _xray(client)
    assert report["schema_version"] == SCHEMA_VERSION and report["tier"] == "xray"
    assert "plan" not in report and "estimate" not in report
    assert report["inputs"]["plan_id"] is None and report["cover"]["watermark"] is None
    assert list(report)[:4] == ["schema_version", "tier", "inputs", "generated_at"]
    assert report["executive_summary"]["status"] == "withheld"
    assert {"glance", "architecture", "risk", "security", "limitations", "next_steps"} <= set(report)


def test_full_includes_plan_estimate_and_records_plan_in_inputs(client):
    _plan_rows(client)
    report = _full(client, calibration_snapshot_id="snap1", pricing_version="p1")
    assert report["plan"]["version"] == 2 and report["estimate"] == ESTIMATE
    assert report["inputs"]["plan_id"] == PLAN
    assert (report["inputs"]["plan_version"], report["inputs"]["plan_status"]) == (2, "approved")
    assert (report["inputs"]["calibration_snapshot_id"], report["inputs"]["pricing_version"]) == ("snap1", "p1")
    assert report["cover"]["watermark"] is None
    assert plan_units(report["plan"]) == [
        {"size": 3, "risk_level": None}, {"size": 40, "risk_level": "high"}, {"size": 5, "risk_level": None},
    ]


@pytest.mark.parametrize("status", ["draft", "in_review"])
def test_unapproved_plan_is_watermarked_with_a_limitation(client, status):
    _plan_rows(client, status=status)
    report = _full(client)
    assert report["cover"]["watermark"] == "draft_plan"
    assert report["inputs"]["plan_status"] == status
    assert "plan_not_approved" in {l["code"] for l in report["limitations"]}


@pytest.mark.parametrize("status", ["stale", "rejected"])
def test_full_fails_for_ineligible_plan(client, status):
    _plan_rows(client, status=status)
    with pytest.raises(ReportInputError):
        _full(client)


def test_full_fails_when_plan_is_pinned_to_another_job(client):
    _plan_rows(client, source="job-other")
    with pytest.raises(ReportInputError):
        _full(client)


def test_tier_argument_rules(client):
    with pytest.raises(ValueError):
        build_report_json(client, ORG, tier="bogus", **COMMON)
    with pytest.raises(ValueError):
        _xray(client, plan_id=PLAN)
    with pytest.raises(ReportInputError):
        build_report_json(client, ORG, tier="full", estimate=ESTIMATE, **COMMON)
    with pytest.raises(ReportInputError):
        build_report_json(client, ORG, tier="full", plan_id=PLAN, **COMMON)


def test_limitations_are_merged_deduplicated_and_sorted(client):
    extra = [{"code": "zzz", "text": "last"}, {"code": "aaa", "text": "first"}, {"code": "aaa", "text": "first"}]
    limits = _xray(client, extra_limitations=extra)["limitations"]
    codes = [l["code"] for l in limits]
    assert codes == sorted(codes) and codes.count("aaa") == 1


def test_same_inputs_give_byte_equal_json_and_generated_at_may_differ(client):
    _plan_rows(client)
    first = canonical_json(_full(client))
    assert first == canonical_json(_full(client))
    later = build_report_json(client, ORG, tier="full", plan_id=PLAN, estimate=ESTIMATE, **{**COMMON, "generated_at": "2099-01-01T00:00:00Z"})
    strip = lambda r: {k: v for k, v in r.items() if k != "generated_at"}  # noqa: E731
    assert canonical_json(strip(later)) == canonical_json(strip(_full(client)))
