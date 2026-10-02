"""Estimator config loader (design_report.md §7 "Config schema").

`load_config()` parses `estimator.toml`, validates it and returns `(config, config_hash)`. The hash
is the sha256 of the canonical JSON of the parsed values, so it changes when any value does and not
when comments or whitespace do; it is recorded in each report's `inputs`. The estimator itself is
pure and takes the parsed dict (`estimator.py`).
"""

import hashlib
import json
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("estimator.toml")
RISKS = ("low", "medium", "high")
PRICING_METHODS = ("markup", "per_unit")


class ConfigError(ValueError):
    """The estimator config is missing a required value or has an invalid one."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _non_negative(table: dict, keys, where: str) -> None:
    for key in keys:
        _require(_number(table.get(key)) and table[key] >= 0, f"{where}.{key} must be a non-negative number")


def validate(config: dict) -> None:
    _require(isinstance(config.get("version"), str) and config["version"], "version must be a non-empty string")

    buckets = config.get("buckets", {})
    edges = buckets.get("size_edges")
    _require(
        isinstance(edges, list) and edges and all(_number(e) for e in edges) and edges == sorted(set(edges)),
        "buckets.size_edges must be a non-empty, strictly increasing list of numbers",
    )
    labels = buckets.get("size_labels")
    if labels is not None:
        _require(
            isinstance(labels, list) and len(labels) == len(edges) + 1 and len(set(labels)) == len(labels),
            "buckets.size_labels needs one distinct label per edge plus one",
        )
    _require(buckets.get("unknown_risk", "medium") in RISKS, "buckets.unknown_risk must be low, medium or high")

    fallback = config.get("defaults", {}).get("fallback")
    _require(isinstance(fallback, dict), "defaults.fallback is required (cold-start numbers)")
    _non_negative(fallback, ("cost_usd", "attempts", "duration_s", "failure_rate"), "defaults.fallback")
    _require(fallback["failure_rate"] <= 1, "defaults.fallback.failure_rate must be at most 1")

    calibration = config.get("calibration", {})
    _require(isinstance(calibration.get("min_samples"), int) and calibration["min_samples"] >= 1, "calibration.min_samples must be a positive integer")
    _require(_number(calibration.get("unpriced_threshold")) and 0 <= calibration["unpriced_threshold"] <= 1, "calibration.unpriced_threshold must be between 0 and 1")

    simulation = config.get("simulation", {})
    for key in ("seed", "runs", "concurrency"):
        _require(isinstance(simulation.get(key), int) and simulation[key] >= (0 if key == "seed" else 1), f"simulation.{key} must be an integer (seed >= 0, others >= 1)")

    hours = config.get("rework", {}).get("hours_per_failed_unit", {})
    _require(set(hours) == set(RISKS), "rework.hours_per_failed_unit needs low, medium and high")
    _non_negative(hours, RISKS, "rework.hours_per_failed_unit")

    manual = config.get("manual_effort", {})
    _require(isinstance(manual.get("per_entity"), dict) and manual["per_entity"], "manual_effort.per_entity is required")
    _non_negative(manual["per_entity"], manual["per_entity"], "manual_effort.per_entity")
    multipliers = manual.get("risk_multiplier", {})
    _require(set(multipliers) == set(RISKS), "manual_effort.risk_multiplier needs low, medium and high")
    _non_negative(multipliers, RISKS, "manual_effort.risk_multiplier")
    _non_negative(manual, ("per_facade_cycle", "per_oversized_unit"), "manual_effort")
    _require(_number(manual.get("p90_factor")) and manual["p90_factor"] >= 1, "manual_effort.p90_factor must be at least 1")

    pricing = config.get("pricing")
    if pricing is not None:
        method = pricing.get("method", "markup")
        _require(method in PRICING_METHODS, f"pricing.method must be one of {PRICING_METHODS}")
        _require(isinstance(pricing.get("currency", "USD"), str), "pricing.currency must be a string")
        _non_negative(pricing, ("markup" if method == "markup" else "unit_rate", "rounding", "minimum"), "pricing")

    sharing = config.get("sharing", {})
    _require(
        isinstance(sharing.get("default_expiry_days"), int) and isinstance(sharing.get("max_expiry_days"), int)
        and 1 <= sharing["default_expiry_days"] <= sharing["max_expiry_days"],
        "sharing needs integer default_expiry_days <= max_expiry_days, both at least 1",
    )

    threshold = config.get("quality", {}).get("sparse_calls_per_function")
    _require(_number(threshold) and threshold >= 0, "quality.sparse_calls_per_function must be a non-negative number")


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_config(path: Path | str | None = None) -> tuple[dict, str]:
    """`(config, hash)`. Raises `ConfigError` on an invalid file (and `OSError`/`tomllib.TOMLDecodeError` on an unreadable one)."""
    with open(path or DEFAULT_PATH, "rb") as handle:
        config = tomllib.load(handle)
    validate(config)
    return config, config_hash(config)
