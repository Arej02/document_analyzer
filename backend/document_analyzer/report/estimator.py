"""Assessment Report estimator (design_report.md §7, specs_report.md §6).

Pure: data in, numbers out. No DB, no rendering, no clock. `config` is the
parsed `estimator.toml` as a plain dict (loading and hashing is `config.py`).

The output has two top-level parts. `customer` is the only part that may ever
reach `report_json.estimate`; it never carries a cost field. `internal` (AI
cost, unpriced share) goes to `report_internal_estimates` only.
"""

import heapq
import math
import random

DEFAULT_SIZE_LABELS = ("xs", "s", "m", "l", "xl", "xxl")
RISK_LEVELS = ("low", "medium", "high")
FAILED_VERDICTS = frozenset({"failed_verification", "error"})


# --- buckets -----------------------------------------------------------------


def size_label(size, config: dict) -> tuple[str, bool]:
    """(label, known). Size `<=` an edge falls in that edge's bucket."""
    buckets = config.get("buckets", {})
    edges = list(buckets.get("size_edges", []))
    labels = list(buckets.get("size_labels") or DEFAULT_SIZE_LABELS[: len(edges) + 1])
    if size is None:
        return buckets.get("unknown_size", labels[len(labels) // 2]), False
    for edge, label in zip(edges, labels):
        if size <= edge:
            return label, True
    return labels[len(edges)], True


def bucket_key(size_label: str, risk: str) -> str:
    return f"{size_label}|{risk}"


def bucket_units(units: list[dict], config: dict) -> tuple[list[dict], list[str]]:
    """Each unit (`{size, risk_level}`) gets a size bucket x risk. A unit with no
    report uses the config's `unknown_risk`; a null size uses `unknown_size`.
    Returns (bucketed units in input order, limitations)."""
    unknown_risk = config.get("buckets", {}).get("unknown_risk", "medium")
    out, no_risk, no_size = [], 0, 0
    for unit in units:
        risk = unit.get("risk_level")
        if risk not in RISK_LEVELS:
            risk = unknown_risk
            no_risk += 1
        label, known = size_label(unit.get("size"), config)
        no_size += 0 if known else 1
        out.append({"size": label, "risk": risk, "key": bucket_key(label, risk)})
    limitations = []
    if no_risk:
        limitations.append(f"{no_risk} unit(s) had no risk report and were assumed {unknown_risk} risk.")
    if no_size:
        limitations.append(f"{no_size} unit(s) had no recorded size and were placed in a default size bucket.")
    return out, limitations


# --- calibration ---------------------------------------------------------------


def build_calibration(rows: list[dict]) -> dict:
    """Aggregate pooled calibration rows (`size`, `risk`, `cost_usd`, `attempts`,
    `duration_s`, `verdict`) into per-bucket empirical samples. Rows with
    `cost_usd = null` are dropped and counted. Nothing identifying is kept: no
    org, project, path or unit id, only the values."""
    buckets: dict[str, list[dict]] = {}
    unpriced = 0
    for row in rows:
        if row.get("cost_usd") is None:
            unpriced += 1
            continue
        buckets.setdefault(bucket_key(row["size"], row["risk"]), []).append(
            {
                "cost_usd": float(row["cost_usd"]),
                "attempts": row.get("attempts"),
                "duration_s": row.get("duration_s"),
                "failed": row.get("verdict") in FAILED_VERDICTS,
            }
        )
    return {
        "buckets": {key: {"n": len(samples), "samples": samples} for key, samples in sorted(buckets.items())},
        "unpriced": unpriced,
        "rows": len(rows),
    }


# --- simulation ------------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile, so p50 <= p90 always holds."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def _range(values: list[float], digits: int) -> dict:
    ordered = sorted(values)
    return {"p50": round(_percentile(ordered, 0.5), digits), "p90": round(_percentile(ordered, 0.9), digits)}


def _makespan(durations: list[float], concurrency: int) -> float:
    """Units run in plan order; each goes to the first free worker."""
    workers = [0.0] * max(1, concurrency)
    heapq.heapify(workers)
    for duration in durations:
        heapq.heappush(workers, heapq.heappop(workers) + duration)
    return max(workers)


def _default_for(config: dict, size: str, risk: str) -> dict:
    return config.get("defaults", {}).get(size, {}).get(risk) or config.get("defaults", {}).get("fallback", {})


def simulate(units: list[dict], calibration: dict, config: dict) -> dict:
    """Seeded Monte Carlo over the plan's units. Returns `{"customer", "internal"}`."""
    sim = config.get("simulation", {})
    seed, runs, concurrency = sim.get("seed", 1), sim.get("runs", 2000), sim.get("concurrency", 1)
    min_samples = config.get("calibration", {}).get("min_samples", 20)
    unpriced_threshold = config.get("calibration", {}).get("unpriced_threshold", 0.2)
    rework_hours = config.get("rework", {}).get("hours_per_failed_unit", {})

    bucketed, limitations = bucket_units(units, config)
    cal_buckets = calibration.get("buckets", {})

    # Per-bucket model: history when it has enough samples, else config defaults.
    models: dict[str, dict] = {}
    for unit in bucketed:
        key = unit["key"]
        if key in models:
            continue
        hist = cal_buckets.get(key, {"n": 0, "samples": []})
        default = _default_for(config, unit["size"], unit["risk"])
        models[key] = {
            "size": unit["size"],
            "risk": unit["risk"],
            "n": hist["n"],
            "historical": hist["n"] >= min_samples,
            "samples": hist["samples"],
            "default": default,
            "units": 0,
        }
    for unit in bucketed:
        models[unit["key"]]["units"] += 1

    rng = random.Random(seed)
    costs, runtimes, reworks, fails = [], [], [], []
    for _ in range(runs):
        cost = rework = 0.0
        failed_n = 0
        durations = []
        for unit in bucketed:
            m = models[unit["key"]]
            d = m["default"]
            if m["historical"]:
                s = rng.choice(m["samples"])
                cost += s["cost_usd"]
                durations.append(s["duration_s"] if s["duration_s"] is not None else d.get("duration_s", 0.0))
                failed = s["failed"]
            else:
                cost += d.get("cost_usd", 0.0)
                durations.append(d.get("duration_s", 0.0))
                failed = rng.random() < d.get("failure_rate", 0.0)
            if failed:
                failed_n += 1
                rework += rework_hours.get(unit["risk"], 0.0)
        costs.append(cost)
        runtimes.append(_makespan(durations, concurrency))
        reworks.append(rework)
        fails.append(failed_n)

    historical_units = sum(m["units"] for m in models.values() if m["historical"])
    default_units = len(bucketed) - historical_units
    basis = "defaults" if historical_units == 0 else "historical" if default_units == 0 else "mixed"
    if default_units:
        limitations.append(
            f"{default_units} of {len(bucketed)} unit(s) were estimated from defaults (fewer than {min_samples} comparable past units)."
        )

    unpriced = calibration.get("unpriced", 0)
    rows = calibration.get("rows", 0)
    unpriced_share = unpriced / rows if rows else 0.0
    overall_n = sum(m["n"] for m in models.values() if m["historical"])
    bucket_rows = [
        {"size": m["size"], "risk": m["risk"], "units": m["units"], "n": m["n"], "source": "historical" if m["historical"] else "defaults"}
        for _, m in sorted(models.items())
    ]

    customer = {
        "units": len(bucketed),
        "basis": basis,
        "n": overall_n,
        "units_from_history": historical_units,
        "units_from_defaults": default_units,
        "runtime_s": _range(runtimes, 0),
        "rework_hours": _range(reworks, 1),
        "failed_units": _range([float(f) for f in fails], 0),
        "concurrency": concurrency,
        "rework_hours_per_failed_unit": dict(rework_hours),
        "buckets": bucket_rows,
        "limitations": limitations,
    }
    internal = {
        "ai_cost_usd": _range(costs, 4),
        "n": overall_n,
        "unpriced_excluded": unpriced,
        "unpriced_share": round(unpriced_share, 4),
        "low_confidence": unpriced_share > unpriced_threshold,
        "buckets": [{**row, "median_cost_usd": _median_cost(models[bucket_key(row["size"], row["risk"])])} for row in bucket_rows],
    }
    return {"customer": customer, "internal": internal}


def _median_cost(model: dict) -> float:
    if model["historical"]:
        return round(_percentile(sorted(s["cost_usd"] for s in model["samples"]), 0.5), 4)
    return model["default"].get("cost_usd", 0.0)


# --- manual effort ---------------------------------------------------------------


def manual_effort(facts: dict, config: dict) -> dict:
    """Developer-days heuristic. `facts`: `entities` {type: count}, `risk_counts`
    {low|medium|high: n} (optional), `facade_cycles`, `oversized_units`. Always a
    heuristic; inputs and coefficients are returned for the report footnote."""
    cfg = config.get("manual_effort", {})
    per_entity = cfg.get("per_entity", {})
    multipliers = cfg.get("risk_multiplier", {})
    entities = facts.get("entities", {})
    limitations = []

    base = sum(per_entity.get(kind, 0.0) * count for kind, count in sorted(entities.items()))
    skipped = sorted(k for k in entities if k not in per_entity and entities[k])
    if skipped:
        limitations.append("No coefficient for entity type(s): " + ", ".join(skipped) + ".")

    risk_counts = {r: facts.get("risk_counts", {}).get(r, 0) for r in RISK_LEVELS}
    total_risk = sum(risk_counts.values())
    if total_risk:
        multiplier = sum(multipliers.get(r, 1.0) * n for r, n in risk_counts.items()) / total_risk
    else:
        multiplier = multipliers.get("medium", 1.0)
        limitations.append("No risk levels recorded; a medium-risk multiplier was assumed.")

    facades = facts.get("facade_cycles", 0)
    oversized = facts.get("oversized_units", 0)
    p50 = base * multiplier + cfg.get("per_facade_cycle", 0.0) * facades + cfg.get("per_oversized_unit", 0.0) * oversized
    p90 = p50 * cfg.get("p90_factor", 1.5)
    return {
        "label": "Heuristic",
        "developer_days": {"p50": round(p50, 1), "p90": round(p90, 1)},
        "inputs": {
            "entities": dict(sorted(entities.items())),
            "risk_counts": risk_counts,
            "facade_cycles": facades,
            "oversized_units": oversized,
        },
        "coefficients": {
            "per_entity": dict(per_entity),
            "risk_multiplier": dict(multipliers),
            "per_facade_cycle": cfg.get("per_facade_cycle", 0.0),
            "per_oversized_unit": cfg.get("per_oversized_unit", 0.0),
            "p90_factor": cfg.get("p90_factor", 1.5),
            "applied_risk_multiplier": round(multiplier, 3),
        },
        "limitations": limitations,
    }


# --- quote -------------------------------------------------------------------------


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step) * step if step and step > 0 else value


def quote(estimate: dict, config: dict) -> dict | None:
    """Price range, or `None` without `[pricing]`. Derived from the internal cost
    (`markup`) or the unit count (`per_unit`) and rounded up to the configured
    precision so the cost can't be reverse-engineered. Carries no cost field."""
    pricing = config.get("pricing")
    if not pricing:
        return None
    method = pricing.get("method", "markup")
    if method == "per_unit":
        base = pricing.get("unit_rate", 0.0) * estimate["customer"]["units"]
        low = high = base
    else:
        cost = estimate["internal"]["ai_cost_usd"]
        factor = 1.0 + pricing.get("markup", 0.0)
        low, high = cost["p50"] * factor, cost["p90"] * factor
    step = pricing.get("rounding", 1)
    minimum = pricing.get("minimum", 0.0)
    low, high = max(_round_up(low, step), minimum), max(_round_up(high, step), minimum)
    return {"currency": pricing.get("currency", "USD"), "method": method, "low": low, "high": high}
