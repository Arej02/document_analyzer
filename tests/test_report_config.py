"""report/config.py — the shipped estimator.toml loads, hashes stably and rejects bad values."""

import copy

import pytest

from document_analyzer.report.config import ConfigError, config_hash, load_config, validate


def test_shipped_config_loads_and_hash_is_stable():
    config, digest = load_config()
    assert config["version"] == "1" and len(digest) == 64
    assert load_config()[1] == digest
    assert "pricing" not in config  # no [pricing] means no price in the report


def test_hash_changes_with_any_value_but_not_key_order():
    config, digest = load_config()
    reordered = dict(reversed(list(config.items())))
    assert config_hash(reordered) == digest
    changed = copy.deepcopy(config)
    changed["simulation"]["seed"] = 2
    assert config_hash(changed) != digest


def test_pricing_is_optional_but_validated_when_present():
    config, _ = load_config()
    config["pricing"] = {"method": "markup", "markup": 3.0, "rounding": 1000, "minimum": 0}
    validate(config)
    config["pricing"] = {"method": "per_unit", "markup": 3.0}
    with pytest.raises(ConfigError, match="unit_rate"):
        validate(config)
    config["pricing"] = {"method": "barter"}
    with pytest.raises(ConfigError, match="method"):
        validate(config)


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda c: c["buckets"].update(size_edges=[8, 3]), "size_edges"),
        (lambda c: c["buckets"].update(size_labels=["a"]), "size_labels"),
        (lambda c: c["defaults"].pop("fallback"), "defaults.fallback"),
        (lambda c: c["defaults"]["fallback"].update(failure_rate=1.5), "failure_rate"),
        (lambda c: c["calibration"].update(min_samples=0), "min_samples"),
        (lambda c: c["simulation"].update(runs=0), "simulation.runs"),
        (lambda c: c["rework"]["hours_per_failed_unit"].pop("high"), "hours_per_failed_unit"),
        (lambda c: c["manual_effort"]["risk_multiplier"].update(low=-1), "risk_multiplier"),
        (lambda c: c["sharing"].update(default_expiry_days=100), "sharing"),
        (lambda c: c.update(version=""), "version"),
    ],
)
def test_invalid_values_are_rejected(mutate, message):
    config, _ = load_config()
    mutate(config)
    with pytest.raises(ConfigError, match=message):
        validate(config)
