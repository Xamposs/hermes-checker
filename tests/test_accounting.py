"""Unit tests for the accounting helpers."""
from __future__ import annotations

import pytest

from hermes_checker.accounting import (
    PricingEntry,
    PricingTable,
    Profile,
    cache_hit_ratio,
    compute_cost,
    extract_usage_summary,
    sanitize_dict,
    sanitize_text,
    tokens_per_second,
)


def test_extract_usage_summary_provider_measured() -> None:
    summary = extract_usage_summary({
        "input_tokens": 80,
        "output_tokens": 20,
        "cache_read_tokens": 200,
        "cache_write_tokens": 5,
        "reasoning_tokens": 7,
    })
    assert summary.input_tokens.value == 80
    assert summary.input_tokens.provenance == "PROVIDER_MEASURED"
    assert summary.cache_read_tokens.value == 200
    assert summary.prompt_tokens.value == 285
    assert summary.prompt_tokens.provenance == "LOCALLY_CALCULATED"
    assert summary.total_tokens.value == 305
    assert summary.reasoning_tokens.value == 7


def test_extract_usage_summary_unavailable() -> None:
    s = extract_usage_summary(None)
    assert s.input_tokens.value is None
    assert s.input_tokens.provenance == "UNAVAILABLE"


def test_extract_usage_summary_accepts_aliases() -> None:
    s = extract_usage_summary({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cached_tokens": 50,
    })
    assert s.input_tokens.value is None  # no input_tokens field
    assert s.output_tokens.value == 20
    assert s.cache_read_tokens.value == 50


def test_cache_hit_ratio() -> None:
    s = extract_usage_summary({"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50})
    assert cache_hit_ratio(s) == 50 / 150
    s2 = extract_usage_summary(None)
    assert cache_hit_ratio(s2) is None


def test_tokens_per_second() -> None:
    s = extract_usage_summary({"input_tokens": 100, "output_tokens": 200})
    assert tokens_per_second(s, duration_s=2.0) == 100.0
    assert tokens_per_second(s, duration_s=0) is None
    s2 = extract_usage_summary(None)
    assert tokens_per_second(s2, duration_s=1.0) is None


def test_compute_cost_basic() -> None:
    entry = PricingEntry(
        model="x",
        input_per_million_usd=1.0,
        cached_input_per_million_usd=0.1,
        output_per_million_usd=2.0,
    )
    cost = compute_cost(
        entry,
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=200_000,
    )
    # 1.0 * 1 + 0.1 * 0.2 + 2.0 * 0.5 = 1 + 0.02 + 1 = 2.02
    assert cost == pytest.approx(2.02)


def test_compute_cost_missing_entry_returns_none() -> None:
    assert compute_cost(None, input_tokens=1, output_tokens=1) is None


def test_pricing_table_lookup() -> None:
    table = PricingTable()
    table.add("default", PricingEntry(model="deepseek-v4-flash", input_per_million_usd=0.27,
                                       cached_input_per_million_usd=0.07, output_per_million_usd=1.10))
    prof = table.get("default")
    assert prof is not None
    assert prof.lookup("deepseek-v4-flash") is not None
    # prefix-tolerant lookup
    assert prof.lookup("openrouter/deepseek-v4-flash") is not None


def test_sanitize_text_redacts_known_patterns() -> None:
    sample = "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = sanitize_text(sample)
    assert "sk-abc" not in sanitized
    assert "<redacted>" in sanitized

    sample2 = "OpenAI key: sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    sanitized2 = sanitize_text(sample2)
    assert "<redacted>" in sanitized2


def test_sanitize_dict_redacts_nested() -> None:
    payload = {
        "headers": {"authorization": "Bearer sk-aaaaaaaaaaaaaaaa123456"},
        "messages": [{"role": "user", "content": "no secrets here"}],
        "api_key": "sk-XXXXXXXXXXXXXXXXXXXX",
    }
    cleaned = sanitize_dict(payload)
    # The full Bearer token value is redacted; the prefix may remain.
    assert "sk-aaaaaaaaaaaaaaaa123456" not in str(cleaned["headers"]["authorization"])
    assert "<redacted>" in cleaned["headers"]["authorization"]
    assert cleaned["api_key"] == "<redacted>"
    assert cleaned["messages"][0]["content"] == "no secrets here"


def test_sanitize_text_truncates() -> None:
    long = "x" * 10_000
    out = sanitize_text(long, max_chars=100)
    assert out is not None
    assert out.startswith("x" * 100)
    assert "truncated" in out