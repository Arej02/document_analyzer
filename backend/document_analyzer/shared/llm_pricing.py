"""LLM list-price lookup and cost math — design.md §7.2.

Pure functions: no DB access, no pipeline state. `cost_usd` is a list-price
estimate from `llm_pricing.toml`, not the Azure invoice. A model with no price
entry yields `cost_usd = None` plus one warning, never an exception, so cost
accounting can never fail a job.

Math is `Decimal` throughout. Cached prompt tokens are charged at the cached
rate, the remaining prompt tokens at the input rate, and completion tokens at the
output rate. Reasoning tokens are already included in `completion_tokens` by the
API, so they are recorded elsewhere but never charged a second time here.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import threading
import tomllib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PRICING_PATH = Path(__file__).with_name("llm_pricing.toml")
_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")  # llm_invocations.cost_usd is numeric(12, 6)


@dataclass(frozen=True)
class PriceEntry:
    id: str
    model_prefix: str
    effective_from: dt.date
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    deployment: str | None = None


def _parse_entry(raw: dict) -> PriceEntry:
    return PriceEntry(
        id=str(raw["id"]),
        model_prefix=str(raw["model_prefix"]),
        effective_from=dt.date.fromisoformat(str(raw["effective_from"])),
        input_per_million=Decimal(str(raw["input_per_million"])),
        cached_input_per_million=Decimal(str(raw["cached_input_per_million"])),
        output_per_million=Decimal(str(raw["output_per_million"])),
        deployment=raw.get("deployment"),
    )


@functools.lru_cache(maxsize=8)
def load_price_table(path: Path = DEFAULT_PRICING_PATH) -> tuple[PriceEntry, ...]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    entries = tuple(_parse_entry(raw) for raw in data.get("entry", []))
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate price entry id in {path}")
    return entries


def find_price(
    response_model: str | None,
    deployment: str | None = None,
    *,
    on: dt.date | None = None,
    table: tuple[PriceEntry, ...] | None = None,
) -> PriceEntry | None:
    """The entry that prices a call, or None.

    A deployment-specific entry beats prefix-only entries; otherwise the longest
    matching model prefix wins; ties go to the newest `effective_from` that is
    not in the future relative to `on` (default: today).
    """
    entries = load_price_table() if table is None else table
    today = on or dt.date.today()
    live = [e for e in entries if e.effective_from <= today]

    if deployment:
        by_deployment = [e for e in live if e.deployment == deployment and _matches(e, response_model)]
        if by_deployment:
            return max(by_deployment, key=lambda e: (len(e.model_prefix), e.effective_from))

    generic = [e for e in live if e.deployment is None and _matches(e, response_model)]
    if not generic:
        return None
    return max(generic, key=lambda e: (len(e.model_prefix), e.effective_from))


def _matches(entry: PriceEntry, response_model: str | None) -> bool:
    return bool(response_model) and response_model.startswith(entry.model_prefix)


def compute_cost(
    entry: PriceEntry,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_prompt_tokens: int = 0,
) -> Decimal:
    """USD for one call, rounded half-up to 6 decimal places."""
    cached = min(max(cached_prompt_tokens, 0), prompt_tokens)
    uncached = prompt_tokens - cached
    total = (
        Decimal(uncached) * entry.input_per_million
        + Decimal(cached) * entry.cached_input_per_million
        + Decimal(max(completion_tokens, 0)) * entry.output_per_million
    ) / _MILLION
    return total.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


_warned_models: set[tuple[str | None, str | None]] = set()
_warn_lock = threading.Lock()


def _warn_unpriced_once(response_model: str | None, deployment: str | None) -> None:
    key = (response_model, deployment)
    with _warn_lock:
        if key in _warned_models:
            return
        _warned_models.add(key)
    logger.warning(
        "no price entry for model %r (deployment %r); recording cost_usd as unknown. "
        "Add an entry to shared/llm_pricing.toml.",
        response_model,
        deployment,
    )


def price_call(
    response_model: str | None,
    deployment: str | None,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_prompt_tokens: int | None = None,
    on: dt.date | None = None,
    table: tuple[PriceEntry, ...] | None = None,
) -> tuple[Decimal | None, str | None]:
    """(cost_usd, pricing_version) for one call.

    Returns (None, None) when the model has no price entry. Returns
    (None, <entry id>) when the entry exists but the API reported no usage.
    Never raises on missing data.
    """
    entry = find_price(response_model, deployment, on=on, table=table)
    if entry is None:
        _warn_unpriced_once(response_model, deployment)
        return None, None
    if prompt_tokens is None or completion_tokens is None:
        return None, entry.id
    cost = compute_cost(
        entry,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_prompt_tokens=cached_prompt_tokens or 0,
    )
    return cost, entry.id
