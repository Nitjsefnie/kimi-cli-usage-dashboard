"""MODEL_RATES is the single source of truth for cost in this repo.
Mirrors backend/pricing.py — bump PARSER_VERSION whenever the table changes.
"""
import pytest

from backend import pricing


def test_k2_7_code_rates_match_source():
    r = pricing.rate_for("kimi-k2-7-code")
    assert r == {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00}


def test_k2_6_rates_match_source():
    r = pricing.rate_for("kimi-k2-6")
    assert r == {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00}


def test_unknown_model_falls_back_to_default():
    r = pricing.rate_for("not-a-real-model")
    assert r == pricing.DEFAULT_RATES


def test_rate_lookup_is_substring_match():
    # rate_for iterates MODEL_RATES and returns the first whose key is
    # contained in the supplied model string.
    assert pricing.rate_for("x-kimi-k2-7-code-y") == pricing.MODEL_RATES["kimi-k2-7-code"]
    assert pricing.rate_for("x-kimi-k2-6-y") == pricing.MODEL_RATES["kimi-k2-6"]


def test_k2_7_code_compute_cost_known_vector():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1_000_000, create=0, read=0, output=0,
    )
    assert cost == pytest.approx(0.95, rel=1e-9)


def test_k2_6_read_rate_is_cheaper_than_k2_7_code():
    cost_7 = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=0, create=0, read=1_000_000, output=0,
    )
    cost_6 = pricing.compute_cost(
        "kimi-k2-6",
        fresh=0, create=0, read=1_000_000, output=0,
    )
    assert cost_7 == pytest.approx(0.19, rel=1e-9)
    assert cost_6 == pytest.approx(0.16, rel=1e-9)
    assert cost_6 < cost_7


def test_cache_creation_is_free_for_kimi_models():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=0, create=1_000_000, read=0, output=0,
    )
    assert cost == pytest.approx(0.00, rel=1e-9)


def test_mixed_usage_cost_is_sum_of_buckets():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=100_000, create=50_000, read=200_000, output=10_000,
    )
    expected = (
        100_000 * 0.95 / 1_000_000
        + 50_000 * 0.00 / 1_000_000
        + 200_000 * 0.19 / 1_000_000
        + 10_000 * 4.00 / 1_000_000
    )
    assert cost == pytest.approx(expected, rel=1e-9)
