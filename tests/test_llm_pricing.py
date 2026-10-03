"""shared/llm_pricing.py — list-price lookup and Decimal cost math (design.md section 7.2)."""

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from document_analyzer.shared import llm_pricing as pricing
from document_analyzer.shared.llm_pricing import PriceEntry, compute_cost, find_price, load_price_table, price_call


def entry(id_, prefix="gpt-x", *, inp="2", cached="0.5", out="8", frm=dt.date(2026, 1, 1), deployment=None):
    return PriceEntry(id_, prefix, frm, Decimal(inp), Decimal(cached), Decimal(out), deployment)


TODAY = dt.date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _fresh_warnings():
    pricing._warned_models.clear()


def test_cost_splits_cached_and_uncached_prompt_tokens():
    # 600 uncached * 2 + 400 cached * 0.5 + 500 completion * 8 = 1200 + 200 + 4000 = 5400 per million
    cost = compute_cost(entry("a"), prompt_tokens=1000, completion_tokens=500, cached_prompt_tokens=400)
    assert cost == Decimal("0.005400")


def test_cached_tokens_never_exceed_the_prompt():
    cost = compute_cost(entry("a"), prompt_tokens=100, completion_tokens=0, cached_prompt_tokens=999)
    assert cost == compute_cost(entry("a"), prompt_tokens=100, completion_tokens=0, cached_prompt_tokens=100)
    assert cost == Decimal("0.000050")


def test_reasoning_tokens_are_not_charged_twice():
    # price_call has no reasoning argument at all: they are already inside completion_tokens.
    with_reasoning_inside = price_call(
        "gpt-x", None, prompt_tokens=10, completion_tokens=300, on=TODAY, table=(entry("a"),)
    )
    assert with_reasoning_inside[0] == compute_cost(entry("a"), prompt_tokens=10, completion_tokens=300)


def test_decimal_math_rounds_half_up_to_six_places_without_float_error():
    # 1 token at 0.5 per million = 0.0000005, which rounds half up to 0.000001 (float math gives 0.0).
    e = entry("a", inp="0.5", cached="0", out="0")
    assert compute_cost(e, prompt_tokens=1, completion_tokens=0) == Decimal("0.000001")
    assert compute_cost(e, prompt_tokens=0, completion_tokens=0) == Decimal("0.000000")
    big = entry("b", inp="0.1", cached="0", out="0")
    assert compute_cost(big, prompt_tokens=3_000_000, completion_tokens=0) == Decimal("0.300000")


def test_unknown_model_gives_null_cost_and_one_warning_never_an_error(caplog):
    caplog.set_level(logging.WARNING, logger=pricing.logger.name)
    table = (entry("a"),)

    first = price_call("mystery-model", "dep", prompt_tokens=10, completion_tokens=10, on=TODAY, table=table)
    second = price_call("mystery-model", "dep", prompt_tokens=10, completion_tokens=10, on=TODAY, table=table)

    assert first == (None, None) and second == (None, None)
    assert len([r for r in caplog.records if "no price entry" in r.message]) == 1


def test_missing_usage_gives_null_cost_but_keeps_the_pricing_version():
    assert price_call("gpt-x", None, prompt_tokens=None, completion_tokens=None, on=TODAY, table=(entry("a"),)) == (
        None,
        "a",
    )


def test_none_response_model_is_unknown_not_a_crash():
    assert price_call(None, None, prompt_tokens=1, completion_tokens=1, on=TODAY, table=(entry("a"),)) == (None, None)


def test_the_longest_matching_prefix_wins():
    table = (entry("base", "gpt-x"), entry("mini", "gpt-x-mini", inp="1"))
    assert find_price("gpt-x-mini-2026", None, on=TODAY, table=table).id == "mini"
    assert find_price("gpt-x-2026", None, on=TODAY, table=table).id == "base"


def test_the_newest_effective_entry_wins_and_future_entries_are_ignored():
    table = (
        entry("old", frm=dt.date(2026, 1, 1)),
        entry("new", frm=dt.date(2026, 6, 1), inp="3"),
        entry("future", frm=dt.date(2027, 1, 1), inp="9"),
    )
    assert find_price("gpt-x", None, on=dt.date(2026, 3, 1), table=table).id == "old"
    assert find_price("gpt-x", None, on=TODAY, table=table).id == "new"
    assert find_price("gpt-x", None, on=dt.date(2025, 1, 1), table=table) is None


def test_price_versions_stay_explainable_old_rows_keep_their_entry_id():
    table = (entry("v1", frm=dt.date(2026, 1, 1), inp="2"), entry("v2", frm=dt.date(2026, 6, 1), inp="4"))
    _, before = price_call("gpt-x", None, prompt_tokens=1, completion_tokens=0, on=dt.date(2026, 2, 1), table=table)
    _, after = price_call("gpt-x", None, prompt_tokens=1, completion_tokens=0, on=dt.date(2026, 7, 1), table=table)
    assert (before, after) == ("v1", "v2")


def test_a_deployment_override_beats_prefix_only_entries_for_that_deployment():
    table = (entry("generic"), entry("special", inp="9", deployment="my-dep"))
    assert find_price("gpt-x", "my-dep", on=TODAY, table=table).id == "special"
    assert find_price("gpt-x", "other-dep", on=TODAY, table=table).id == "generic"
    assert find_price("gpt-x", None, on=TODAY, table=table).id == "generic"


def test_a_deployment_entry_does_not_price_a_different_model():
    table = (entry("special", "gpt-y", deployment="my-dep"),)
    assert find_price("gpt-x", "my-dep", on=TODAY, table=table) is None


def test_the_shipped_price_file_loads_and_has_unique_ids():
    # Ships with only a commented template (prices are the operator's to fill in); it must still parse.
    assert isinstance(load_price_table(), tuple)


def test_a_toml_file_is_parsed_into_decimal_entries_and_duplicate_ids_are_rejected(tmp_path: Path):
    good = tmp_path / "p.toml"
    good.write_text(
        '[[entry]]\nid = "a"\nmodel_prefix = "gpt-x"\neffective_from = "2026-01-01"\n'
        'input_per_million = "0.10"\ncached_input_per_million = "0.01"\noutput_per_million = "0.40"\n',
        encoding="utf-8",
    )
    [parsed] = load_price_table(good)
    assert parsed.input_per_million == Decimal("0.10") and parsed.effective_from == dt.date(2026, 1, 1)

    dup = tmp_path / "dup.toml"
    dup.write_text(good.read_text(encoding="utf-8") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_price_table(dup)
