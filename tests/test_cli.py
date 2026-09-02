"""Tests for the CLI argument parser and a few command handlers."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_checker import cli
from hermes_checker.accounting import load_pricing_profile


def test_cli_parses_all_commands() -> None:
    parser = cli._build_parser()
    for cmd in ("doctor", "status", "dashboard", "sessions",
                "install", "uninstall"):
        args = parser.parse_args([cmd])
        assert args.command == cmd


def test_cli_report_parses_options() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["report", "--session", "abc", "--pricing-profile", "openrouter",
                              "--pricing-file", "config/x.yaml"])
    assert args.session == "abc"
    assert args.pricing_profile == "openrouter"


def test_cli_export_supports_json_csv() -> None:
    parser = cli._build_parser()
    for fmt in ("json", "csv"):
        args = parser.parse_args(["export", "--session", "abc", "--format", fmt])
        assert args.format == fmt


def test_parse_simple_yaml_loads_example(tmp_path: Path) -> None:
    example = Path(__file__).parent.parent / "config" / "pricing.example.yaml"
    if not example.exists():
        pytest.skip("pricing.example.yaml missing")
    table = load_pricing_profile(example)
    names = table.profile_names()
    assert "openrouter-2026-09" in names
    profile = table.get("openrouter-2026-09")
    assert profile is not None
    assert profile.lookup("deepseek/deepseek-chat-v4-flash") is not None


def test_parse_simple_yaml_minimal(tmp_path: Path) -> None:
    """Round-trip a minimal pricing YAML through the public loader."""
    text = """
profiles:
  - name: foo
    models:
      bar/baz:
        input_per_million_usd: 1.5
        output_per_million_usd: 7.0
"""
    f = tmp_path / "p.yaml"
    f.write_text(text, encoding="utf-8")
    table = load_pricing_profile(f)
    profile = table.get("foo")
    assert profile is not None
    entry = profile.lookup("bar/baz")
    assert entry is not None
    assert entry.input_per_million_usd == 1.5
    assert entry.output_per_million_usd == 7.0