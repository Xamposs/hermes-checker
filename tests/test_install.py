"""Tests for the install / uninstall helpers."""
from __future__ import annotations

from pathlib import Path

from hermes_checker.install import _inject_enabled, _remove_enabled


def test_inject_enabled_adds_to_empty_config() -> None:
    out = _inject_enabled("")
    assert "plugins:" in out
    assert "enabled:" in out
    assert "hermes-checker" in out


def test_inject_enabled_preserves_existing() -> None:
    text = "model:\n  default: x\nproviders:\n  p: {}\n"
    out = _inject_enabled(text)
    assert "model:" in out
    assert "default: x" in out
    assert "providers:" in out
    assert "hermes-checker" in out


def test_inject_enabled_is_idempotent() -> None:
    text = "plugins:\n  enabled:\n    - hermes-checker\n"
    out = _inject_enabled(text)
    # We don't worry about exact line count, just no duplication
    assert out.count("- hermes-checker") == 1


def test_remove_enabled_strips_entry() -> None:
    text = (
        "plugins:\n"
        "  enabled:\n"
        "    - other-plugin\n"
        "    - hermes-checker\n"
    )
    out = _remove_enabled(text)
    assert "- hermes-checker" not in out
    assert "- other-plugin" in out


def test_remove_enabled_when_missing_is_noop() -> None:
    text = "plugins:\n  enabled:\n    - other-plugin\n"
    out = _remove_enabled(text)
    assert out == text