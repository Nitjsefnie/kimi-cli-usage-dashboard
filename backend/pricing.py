"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in kimi-dash.
Bump PARSER_VERSION when this table changes — every session reparses.

Kimi wire format token categories:
  - fresh  = input_other        (vendor: "input price, cache miss")
  - read   = input_cache_read   (vendor: "input price, cache hit")
  - create = input_cache_creation
  - output = output

No TTL split in Kimi wire format; cache_create is billed at a flat rate.

Every model parse.py can emit MUST have an entry here. The keys share no
substrings, so a missing entry does not raise — rate_for silently returns
DEFAULT_RATES (the cheapest, oldest model) and undercounts cost.
"""
from __future__ import annotations


# Order: most-specific first.
MODEL_RATES = {
    "kimi-k3":        {"fresh": 3.00, "create": 0.00, "read": 0.30, "output": 15.00},
    "kimi-k2-7-code": {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00},
    "kimi-k2-6":      {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00},
}

DEFAULT_RATES = MODEL_RATES["kimi-k2-6"]


def rate_for(model: str) -> dict:
    if not model:
        return DEFAULT_RATES
    for key, rates in MODEL_RATES.items():
        if key in model:
            return rates
    return DEFAULT_RATES


def compute_cost(
    model: str,
    *,
    fresh: int,
    create: int,
    read: int,
    output: int,
) -> float:
    """USD cost for one StatusUpdate token tally."""
    r = rate_for(model)
    return (
        fresh * r["fresh"] / 1_000_000
        + create * r["create"] / 1_000_000
        + read * r["read"] / 1_000_000
        + output * r["output"] / 1_000_000
    )
