"""Tests for the report assembler."""
from __future__ import annotations

from pathlib import Path

from hermes_checker.collector import HookCollector
from hermes_checker.collector.config import CollectorConfig
from hermes_checker.reporting import build_report, render_text
from hermes_checker.storage import Database, DatabasePaths


def _make_session(tmp_path: Path) -> HookCollector:
    paths = DatabasePaths.from_path(tmp_path / "hc.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    return HookCollector(database=db, config=cfg)


def test_report_round_trip(tmp_path: Path) -> None:
    collector = _make_session(tmp_path)
    collector.on_session_start(session_id="r1", profile="default", platform="desktop")
    collector.pre_api_request(
        session_id="r1",
        api_request_id="a1",
        api_call_count=1,
        provider="openrouter",
        model="deepseek/deepseek-chat-v4-flash",
        started_at=1.0,
        messages=[
            {"role": "system", "content": "You are a coding assistant." * 50},
            {"role": "user", "content": "Help me refactor." * 20},
        ],
    )
    collector.post_api_request(
        session_id="r1",
        api_request_id="a1",
        api_call_count=1,
        provider="openrouter",
        model="deepseek/deepseek-chat-v4-flash",
        started_at=1.0, ended_at=2.0, api_duration=1.0,
        usage={"input_tokens": 1000, "output_tokens": 200,
               "cache_read_tokens": 400, "cache_write_tokens": 0,
               "reasoning_tokens": 50},
    )
    collector.pre_tool_call(
        session_id="r1", tool_call_id="tc1", tool_name="terminal",
        args={"command": "pytest"}, started_at=2.0,
    )
    collector.post_tool_call(
        session_id="r1", tool_call_id="tc1", tool_name="terminal",
        result="all passed" * 5000, started_at=2.0, ended_at=2.5,
        duration_ms=500.0, status="ok",
    )

    db = collector.db
    report = build_report(db, "r1")
    text = render_text(report)

    assert "HERMES CHECKER — SESSION REPORT" in text
    assert "Provider prompt" in text
    assert "Component attribution" in text.lower() or "LOCAL ATTRIBUTION" in text
    assert "Cache hit" in text
    assert "deepseek" in text  # model surfaced
    # Two api requests worth of data shouldn't show in a one-request session
    # — but we DO have one request and we expect prompt==1000+400+0=1400
    assert report.totals.prompt_tokens == 1400
    assert report.totals.cache_read_tokens == 400
    assert report.totals.output_tokens == 200
    assert report.totals.reasoning_tokens == 50
    assert any(c.component == "SYSTEM" for c in report.component_breakdown)
    # Command-aware classification (Issue 9) upgrades "pytest" to "test".
    assert any(t.category in ("test", "terminal") for t in report.tool_breakdown)