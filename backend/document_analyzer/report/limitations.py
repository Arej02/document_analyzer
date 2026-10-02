"""Generated assumptions and limitations (specs_report.md §5.8, design_report.md §5).

Pure: turns the quality facts from `collect_quality`, the estimate's customer part and the
executive summary's status into `{"code", "text"}` entries. Never hand-written. The other
§5.8 items (parsing gaps, fallbacks used, a draft plan, summaries that can't be attributed)
are produced by the collectors themselves, next to the data they describe.
"""

DEFAULT_SPARSE_CALLS_PER_FUNCTION = 0.5
DEFAULT_MIN_SAMPLES = 20


def _limit(code: str, text: str) -> dict:
    return {"code": code, "text": text}


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%"


def build_limitations(
    quality: dict,
    config: dict,
    *,
    estimate: dict | None = None,
    executive_summary: dict | None = None,
    calibration_snapshot: dict | None = None,
) -> list[dict]:
    """`estimate` is the estimator's `customer` part; `calibration_snapshot` the snapshot
    dict from `calibration.py` (date range and counts only, no org data)."""
    out = []

    unverified = {lvl: c for lvl, c in quality["summaries_by_level"].items() if c["unverified"]}
    if unverified:
        parts = [f"{lvl} {_pct(c['unverified'], c['total'])} ({c['unverified']} of {c['total']})" for lvl, c in sorted(unverified.items())]
        out.append(_limit("unverified_summaries", "Summaries that could not be fact-checked, by level: " + ", ".join(parts) + "."))

    threshold = config.get("quality", {}).get("sparse_calls_per_function", DEFAULT_SPARSE_CALLS_PER_FUNCTION)
    functions = quality["functions"]
    if functions and quality["resolved_calls"] / functions < threshold:
        out.append(
            _limit(
                "sparse_call_graph",
                f"The call graph is sparse: {quality['resolved_calls']} resolved call(s) across {functions} function(s) "
                f"({quality['resolved_calls'] / functions:.2f} per function, below {threshold}). "
                "Dependency, hub and ordering results may be incomplete.",
            )
        )
    if quality["graphify_edges"] and quality["inferred_edges"]:
        out.append(
            _limit(
                "inferred_edges",
                f"{_pct(quality['inferred_edges'], quality['graphify_edges'])} of relationships found by the multi-language "
                f"parser were inferred rather than read directly ({quality['inferred_edges']} of {quality['graphify_edges']}).",
            )
        )

    if estimate:
        min_samples = config.get("calibration", {}).get("min_samples", DEFAULT_MIN_SAMPLES)
        cold = [b for b in estimate["buckets"] if b["source"] == "defaults"]
        if cold:
            names = ", ".join(f"{b['size']}/{b['risk']} (n={b['n']})" for b in cold)
            out.append(
                _limit(
                    "cold_start_buckets",
                    f"{estimate['units_from_defaults']} of {estimate['units']} unit(s) were estimated from defaults, because "
                    f"fewer than {min_samples} comparable past units exist for: {names}.",
                )
            )
        hours = estimate.get("rework_hours_per_failed_unit") or {}
        history = f"{estimate['n']} past unit(s)" if estimate["n"] else "no past units"
        if calibration_snapshot and calibration_snapshot.get("date_from"):
            history += f", {calibration_snapshot['date_from']} to {calibration_snapshot['date_to']}"
        rework = ", ".join(f"{k} {v} h" for k, v in sorted(hours.items()))
        out.append(
            _limit(
                "estimate_assumptions",
                f"The estimate is based on {history}, assumes {estimate['concurrency']} unit(s) run at a time, and adds rework "
                f"per failed unit ({rework}). Prices may change.",
            )
        )
        out.extend(_limit("estimate_note", text) for text in estimate.get("limitations", []))

    if executive_summary:
        if executive_summary.get("status") == "unverified":
            out.append(_limit("executive_summary_unverified", "The executive summary could not be fully fact-checked and is labelled Unverified."))
        elif executive_summary.get("status") == "withheld":
            out.append(_limit("executive_summary_withheld", "The executive summary was withheld by a content check."))
    return out
