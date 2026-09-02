"""End-to-end tests for the collector."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_checker.collector import HookCollector
from hermes_checker.collector.config import CollectorConfig
from hermes_checker.collector.collector import classify_tool
from hermes_checker.storage import Database, DatabasePaths


@pytest.fixture()
def collector(tmp_path: Path) -> HookCollector:
    paths = DatabasePaths.from_path(tmp_path / "hc.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    return HookCollector(database=db, config=cfg)


def test_classify_tool() -> None:
    assert classify_tool("read_file") == "file_read"
    assert classify_tool("terminal") == "terminal"
    assert classify_tool("bash") == "terminal"
    assert classify_tool("web_search") == "web"
    assert classify_tool("git_diff") == "git"
    assert classify_tool("pytest") == "test"
    assert classify_tool("search") == "search"
    assert classify_tool("write_file") == "file_write"
    assert classify_tool("mcp_server_xyz") == "mcp"
    assert classify_tool("memory_recall") == "memory"
    assert classify_tool("") == "other"
    assert classify_tool("some_unknown_tool") == "other"


def test_session_lifecycle_persists(collector: HookCollector) -> None:
    collector.on_session_start(session_id="s1", platform="desktop", profile="default")
    collector.on_session_end(session_id="s1")
    db = collector.db
    row = db.session("s1")
    assert row is not None
    assert row["ended_at"] is not None


def test_api_request_round_trip_with_usage(collector: HookCollector) -> None:
    collector.on_session_start(session_id="s2")
    collector.pre_api_request(
        session_id="s2",
        api_request_id="r1",
        api_call_count=1,
        provider="opencode",
        model="MiniMax-m3",
        base_url="https://example/v1",
        api_mode="chat_completions",
        streaming=True,
        started_at=100.0,
        messages=[{"role": "system", "content": "You are a helpful assistant."},
                  {"role": "user", "content": "hi"}],
    )
    collector.post_api_request(
        session_id="s2",
        api_request_id="r1",
        api_call_count=1,
        provider="opencode",
        model="MiniMax-m3",
        base_url="https://example/v1",
        api_mode="chat_completions",
        started_at=100.0,
        ended_at=101.5,
        api_duration=1.5,
        first_chunk_at=100.2,
        finish_reason="stop",
        usage={
            "input_tokens": 100,
            "output_tokens": 30,
            "cache_read_tokens": 50,
            "reasoning_tokens": 5,
        },
        assistant_content_chars=60,
        assistant_tool_call_count=0,
    )

    db = collector.db
    rows = db.api_requests_for_session("s2")
    assert len(rows) == 1
    r = rows[0]
    assert r["prompt_tokens"] == 150
    assert r["input_tokens"] == 100
    assert r["output_tokens"] == 30
    assert r["cache_read_tokens"] == 50
    assert r["reasoning_tokens"] == 5
    assert r["ttft_s"] == pytest.approx(0.2, abs=1e-6)
    assert r["cache_hit_ratio"] == pytest.approx(50 / 150)
    # Tokens-per-second: (1.5 - 0.2) generation time
    assert r["tokens_per_second"] == pytest.approx(30 / 1.3, rel=1e-3)

    # Component attribution rows were written
    components = db.prompt_components_for_request(r["id"])
    assert components, "expected attribution rows"


def test_tool_call_round_trip(collector: HookCollector) -> None:
    collector.on_session_start(session_id="s3")
    collector.pre_tool_call(
        session_id="s3",
        tool_call_id="tc-1",
        tool_name="terminal",
        args={"command": "ls -la"},
        started_at=1.0,
    )
    collector.post_tool_call(
        session_id="s3",
        tool_call_id="tc-1",
        tool_name="terminal",
        result="total 24\n-rw-r--r-- 1 x x  1234 Jan 1 00:00 file",
        started_at=1.0,
        ended_at=1.5,
        duration_ms=500.0,
        status="ok",
        exit_code=0,
    )
    rows = collector.db.tool_calls_for_session("s3")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "terminal"
    assert rows[0]["category"] == "terminal"
    assert rows[0]["output_chars"] > 0
    assert rows[0]["exit_code"] == 0


def test_api_request_without_pre_hook_still_records(collector: HookCollector) -> None:
    """A post hook may arrive if a hook chain is interrupted; we must not drop data."""
    collector.on_session_start(session_id="s4")
    collector.post_api_request(
        session_id="s4",
        api_request_id="only-post",
        api_call_count=1,
        provider="openrouter",
        model="deepseek/deepseek-chat",
        started_at=1.0,
        ended_at=2.0,
        api_duration=1.0,
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    rows = collector.db.api_requests_for_session("s4")
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1


def test_findings_emitted_when_analyzer_enabled(tmp_path: Path) -> None:
    paths = DatabasePaths.from_path(tmp_path / "hc.db")
    db = Database(paths)
    cfg = CollectorConfig(
        database_path=str(paths.database),
        run_analyzer=True,
        large_jump_tokens=100,
    )
    collector = HookCollector(database=db, config=cfg)
    collector.on_session_start(session_id="s5")
    # Two API requests with a 200-token jump between them
    collector.post_api_request(
        session_id="s5",
        api_request_id="a",
        provider="p", model="m", started_at=1.0, ended_at=2.0,
        usage={"input_tokens": 100, "output_tokens": 5},
    )
    collector.post_api_request(
        session_id="s5",
        api_request_id="b",
        provider="p", model="m", started_at=3.0, ended_at=4.0,
        usage={"input_tokens": 300, "output_tokens": 5},
    )
    findings = collector.db.findings(session_id="s5")
    assert any(f["finding_kind"] == "large_context_jump" for f in findings)