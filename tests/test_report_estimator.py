"""report/estimator.py — design_report.md §7, specs_report.md §6."""

import json

from document_analyzer.report.estimator import (
    bucket_units,
    build_calibration,
    manual_effort,
    quote,
    simulate,
)

CONFIG = {
    "buckets": {"size_edges": [3, 8, 20], "unknown_risk": "medium", "unknown_size": "s"},
    "defaults": {
        "fallback": {"cost_usd": 5.0, "attempts": 2, "duration_s": 100.0, "failure_rate": 0.0},
        "xs": {"low": {"cost_usd": 1.0, "attempts": 1, "duration_s": 30.0, "failure_rate": 0.0}},
    },
    "calibration": {"min_samples": 3, "unpriced_threshold": 0.2},
    "simulation": {"seed": 7, "runs": 200, "concurrency": 1},
    "rework": {"hours_per_failed_unit": {"low": 2, "medium": 4, "high": 8}},
    "manual_effort": {
        "per_entity": {"function": 0.1, "class": 0.3},
        "risk_multiplier": {"low": 1.0, "medium": 1.5, "high": 2.5},
        "per_facade_cycle": 2.0,
        "per_oversized_unit": 1.0,
    },
}


def _rows(n, *, size="xs", risk="low", cost=1.0, duration=10.0, verdict="passed", attempts=1):
    return [
        {"size": size, "risk": risk, "cost_usd": cost, "attempts": attempts, "duration_s": duration, "verdict": verdict}
        for _ in range(n)
    ]


def _unit(size=2, risk="low"):
    return {"size": size, "risk_level": risk}


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


# --- cold start ----------------------------------------------------------------


def test_empty_calibration_says_defaults():
    est = simulate([_unit(), _unit()], build_calibration([]), CONFIG)["customer"]
    assert est["basis"] == "defaults"
    assert est["units_from_defaults"] == 2 and est["units_from_history"] == 0
    assert est["n"] == 0
    assert any("defaults" in text for text in est["limitations"])
    # xs|low defaults: 30 s each, concurrency 1, no failures.
    assert est["runtime_s"] == {"p50": 60.0, "p90": 60.0}


def test_below_min_samples_still_uses_defaults_but_reports_bucket_n():
    cal = build_calibration(_rows(2, duration=999.0))  # min_samples is 3
    est = simulate([_unit()], cal, CONFIG)["customer"]
    assert est["basis"] == "defaults"
    assert est["runtime_s"]["p50"] == 30.0  # the default, not the 999 s history
    assert est["buckets"] == [{"size": "xs", "risk": "low", "units": 1, "n": 2, "source": "defaults"}]


# --- normal case, hand-computed -------------------------------------------------


def test_historical_buckets_match_hand_computed_numbers():
    cal = build_calibration(_rows(3, cost=1.5, duration=10.0))
    est = simulate([_unit(), _unit()], cal, CONFIG)
    # 2 units x $1.5, 2 x 10 s in sequence, nothing failed.
    assert est["internal"]["ai_cost_usd"] == {"p50": 3.0, "p90": 3.0}
    assert est["customer"]["runtime_s"] == {"p50": 20.0, "p90": 20.0}
    assert est["customer"]["rework_hours"] == {"p50": 0.0, "p90": 0.0}
    assert est["customer"]["basis"] == "historical"
    assert est["customer"]["n"] == est["internal"]["n"] == 3


def test_concurrency_shortens_wall_clock():
    cal = build_calibration(_rows(3, duration=10.0))
    config = {**CONFIG, "simulation": {**CONFIG["simulation"], "concurrency": 2}}
    est = simulate([_unit(), _unit()], cal, config)["customer"]
    assert est["runtime_s"]["p50"] == 10.0 and est["concurrency"] == 2


def test_p50_not_above_p90_and_n_reported():
    rows = [
        {"size": "xs", "risk": "low", "cost_usd": c, "attempts": 1, "duration_s": d, "verdict": "passed"}
        for c, d in [(1, 5), (2, 10), (3, 20), (4, 40), (10, 80)]
    ]
    est = simulate([_unit()] * 6, build_calibration(rows), CONFIG)
    for part, field in (("customer", "runtime_s"), ("customer", "rework_hours"), ("internal", "ai_cost_usd")):
        assert est[part][field]["p50"] <= est[part][field]["p90"]
    assert est["customer"]["n"] == 5
    assert est["customer"]["buckets"][0]["n"] == 5


# --- mixed default and historical ------------------------------------------------


def test_mixed_buckets_count_defaults_versus_history():
    cal = build_calibration(_rows(3, cost=2.0, duration=10.0))  # xs|low only
    units = [_unit(2, "low"), _unit(5, "low")]  # xs|low (history), s|low (defaults)
    est = simulate(units, cal, CONFIG)
    customer = est["customer"]
    assert customer["basis"] == "mixed"
    assert (customer["units_from_history"], customer["units_from_defaults"]) == (1, 1)
    assert {b["size"]: b["source"] for b in customer["buckets"]} == {"xs": "historical", "s": "defaults"}
    # 10 s from history + 100 s default; $2 history + $5 default.
    assert customer["runtime_s"]["p50"] == 110.0
    assert est["internal"]["ai_cost_usd"]["p50"] == 7.0


# --- missing data -----------------------------------------------------------------


def test_missing_risk_and_size_use_config_defaults_and_say_so():
    bucketed, limitations = bucket_units([{"size": None, "risk_level": None}], CONFIG)
    assert (bucketed[0]["size"], bucketed[0]["risk"]) == ("s", "medium")
    assert len(limitations) == 2
    assert any("risk" in text for text in limitations) and any("size" in text for text in limitations)


def test_size_edges_are_inclusive_upper_bounds():
    sizes = [1, 3, 4, 8, 9, 20, 21]
    labels = [b["size"] for b in bucket_units([_unit(s) for s in sizes], CONFIG)[0]]
    assert labels == ["xs", "xs", "s", "s", "m", "m", "l"]


def test_unpriced_rows_are_dropped_counted_and_flag_low_confidence():
    rows = _rows(3) + _rows(2, cost=None)
    cal = build_calibration(rows)
    assert cal["unpriced"] == 2 and cal["rows"] == 5
    assert cal["buckets"]["xs|low"]["n"] == 3
    internal = simulate([_unit()], cal, CONFIG)["internal"]
    assert internal["unpriced_excluded"] == 2
    assert internal["unpriced_share"] == 0.4
    assert internal["low_confidence"] is True


def test_few_unpriced_rows_stay_confident():
    cal = build_calibration(_rows(9) + _rows(1, cost=None))  # 10% <= 20%
    assert simulate([_unit()], cal, CONFIG)["internal"]["low_confidence"] is False


# --- determinism -------------------------------------------------------------------


def test_same_inputs_give_identical_output_and_seed_changes_draws():
    rows = [
        {"size": "xs", "risk": "low", "cost_usd": c, "attempts": 1, "duration_s": c * 10, "verdict": "passed"}
        for c in range(1, 30)
    ]
    cal = build_calibration(rows)
    units = [_unit() for _ in range(5)]
    first = simulate(units, cal, CONFIG)
    assert json.dumps(first, sort_keys=True) == json.dumps(simulate(units, cal, CONFIG), sort_keys=True)
    other = {**CONFIG, "simulation": {**CONFIG["simulation"], "seed": 8}}
    assert simulate(units, cal, other)["internal"] != first["internal"]


# --- failures are not free ---------------------------------------------------------


def test_observed_failure_rate_adds_rework_by_risk():
    cal = build_calibration(_rows(3, risk="high", verdict="failed_verification"))
    est = simulate([_unit(2, "high"), _unit(2, "high")], cal, CONFIG)["customer"]
    assert est["failed_units"] == {"p50": 2.0, "p90": 2.0}
    assert est["rework_hours"] == {"p50": 16.0, "p90": 16.0}  # 2 x 8 h


def test_default_failure_rate_adds_rework_and_error_counts_as_failed():
    config = {**CONFIG, "defaults": {"fallback": {"cost_usd": 1, "duration_s": 1, "failure_rate": 1.0}}}
    est = simulate([_unit(2, "low")] * 3, build_calibration([]), config)["customer"]
    assert est["rework_hours"]["p50"] == 6.0  # 3 x 2 h
    cal = build_calibration(_rows(3, verdict="error"))
    assert simulate([_unit()], cal, CONFIG)["customer"]["failed_units"]["p50"] == 1.0


# --- quote -------------------------------------------------------------------------


def test_no_pricing_gives_no_quote():
    est = simulate([_unit()], build_calibration([]), CONFIG)
    assert "pricing" not in CONFIG and quote(est, CONFIG) is None


def test_markup_quote_is_rounded_up_and_respects_minimum():
    est = simulate([_unit(), _unit()], build_calibration(_rows(3, cost=1.5)), CONFIG)  # cost 3.0
    config = {**CONFIG, "pricing": {"currency": "EUR", "method": "markup", "markup": 2.0, "rounding": 10}}
    assert quote(est, config) == {"currency": "EUR", "method": "markup", "low": 10, "high": 10}  # 9 -> 10
    config["pricing"]["minimum"] = 500
    assert quote(est, config)["low"] == 500


def test_per_unit_quote_and_quote_has_no_cost_field():
    est = simulate([_unit()] * 3, build_calibration([]), CONFIG)
    config = {**CONFIG, "pricing": {"method": "per_unit", "unit_rate": 120.0, "rounding": 100}}
    result = quote(est, config)
    assert (result["low"], result["high"]) == (400, 400)  # 360 -> 400
    assert not any("cost" in key for key in result)


# --- manual effort -----------------------------------------------------------------


def test_manual_effort_matches_hand_computed_value():
    facts = {
        "entities": {"function": 10, "class": 2},
        "risk_counts": {"low": 1, "high": 1},
        "facade_cycles": 1,
        "oversized_units": 2,
    }
    result = manual_effort(facts, CONFIG)
    # (10*0.1 + 2*0.3) = 1.6; multiplier (1.0 + 2.5) / 2 = 1.75 -> 2.8; + 2.0 + 2*1.0 = 6.8; p90 x1.5
    assert result["label"] == "Heuristic"
    assert result["developer_days"] == {"p50": 6.8, "p90": 10.2}
    assert result["coefficients"]["per_entity"] == {"function": 0.1, "class": 0.3}
    assert result["limitations"] == []


def test_manual_effort_without_risk_or_coefficient_says_so():
    result = manual_effort({"entities": {"function": 10, "widget": 4}}, CONFIG)
    assert result["developer_days"]["p50"] == 1.5  # 1.0 x medium 1.5
    assert len(result["limitations"]) == 2


# --- customer / internal split -------------------------------------------------------


def test_customer_part_has_no_cost_field():
    cal = build_calibration(_rows(3) + _rows(2, cost=None))
    est = simulate([_unit(), _unit(5, "high")], cal, CONFIG)
    assert set(est) == {"customer", "internal"}
    assert not [key for key in _all_keys(est["customer"]) if "cost" in key.lower()]
    assert "ai_cost_usd" in est["internal"]
