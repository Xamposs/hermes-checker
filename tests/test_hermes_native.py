"""Tests for the Hermes-native prompt breakdown wrapper.

These tests are skipped when Hermes is not importable in the test
process; on a real Hermes install they exercise the actual
``compute_prompt_breakdown`` path.
"""
from __future__ import annotations

import pytest

from hermes_checker import PROVENANCE_LOCALLY_ESTIMATED
from hermes_checker.hermes_native import (
    HermesNativeBridge,
    NativeBreakdown,
    get_native_bridge,
    local_estimate_from_payload,
)


def test_local_estimate_from_payload_basic() -> None:
    """The local fallback must produce a usable breakdown tagged as estimate."""
    b = local_estimate_from_payload(
        [
            {"role": "system", "content": "You are a coding assistant." * 30},
            {"role": "user", "content": "Help me refactor." * 20},
            {"role": "assistant", "content": "Sure, here you go." * 10},
        ],
        model="MiniMax-m3",
    )
    assert b.hermes_native is False
    assert b.provenance == PROVENANCE_LOCALLY_ESTIMATED
    assert b.tiers["volatile"]["chars"] > 0
    assert b.sections
    # Each section must be a (label, chars, bytes) triple.
    for section in b.sections:
        assert "label" in section
        assert "chars" in section
        assert "bytes" in section


def test_local_estimate_handles_empty_input() -> None:
    b = local_estimate_from_payload([])
    assert b.hermes_native is False
    assert b.tiers == {
        "stable": {"chars": 0, "bytes": 0, "tokens_est": 0},
        "context": {"chars": 0, "bytes": 0, "tokens_est": 0},
        "volatile": {"chars": 0, "bytes": 0, "tokens_est": 0},
    }


def test_get_native_bridge_returns_bridge() -> None:
    """The bridge is cached; two calls with the same home return the same instance."""
    from pathlib import Path

    # hermes_home defaults to None which triggers the Hermes auto-detect.
    # In CI / outside Hermes' venv the import will fail, but the bridge
    # must still be constructible and must answer ``compute()`` safely.
    b1 = get_native_bridge(Path("/nonexistent"))
    b2 = get_native_bridge(Path("/nonexistent"))
    assert b1 is b2
    breakdown = b1.compute()
    # Either hermes_native=True (real Hermes is reachable) or
    # hermes_native=False with a populated ``reason`` field.
    if not breakdown.hermes_native:
        assert breakdown.reason  # we got some explanation


def test_native_breakdown_from_hermes_payload() -> None:
    """The converter must handle the real Hermes dict shape."""
    fake_payload = {
        "platform": "cli",
        "model": "MiniMax-m3",
        "system_prompt": {"chars": 4500, "bytes": 4500},
        "skills_index": {"chars": 800, "bytes": 800},
        "memory": {"chars": 200, "bytes": 200},
        "user_profile": {"chars": 100, "bytes": 100},
        "tools": {"count": 42, "json_bytes": 12000},
        "sections": [
            ["stable (identity/guidance/skills)", 2000, 2000],
            ["context (AGENTS.md/cwd files)", 1000, 1000],
            ["volatile (memory/profile/timestamp)", 1500, 1500],
        ],
        "skills_breakdown": [
            {"name": "alpha", "index_line_bytes": 100, "skill_md_bytes": 200},
        ],
        "toolsets_breakdown": [
            {"name": "hermes-cli", "tool_count": 10, "schema_bytes": 3000},
        ],
    }
    b = NativeBreakdown.from_hermes_payload(fake_payload)
    assert b.hermes_native is True
    assert b.model == "MiniMax-m3"
    assert b.platform == "cli"
    # Tiers are recovered from the sections list by label.
    assert b.tiers["stable"]["chars"] == 2000
    assert b.tiers["context"]["chars"] == 1000
    assert b.tiers["volatile"]["chars"] == 1500
    assert b.skills_index["chars"] == 800
    assert b.tools["count"] == 42
    assert b.sections[0]["label"].startswith("stable")
    # Token estimates must be populated by the local tokenizer pass.
    assert b.tiers["stable"]["tokens_est"] > 0
    assert b.tools.get("json_tokens_est", 0) > 0
