"""Tests for the V1.1 collector and reporting changes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_checker.accounting.tokenizer import Tokenizer
from hermes_checker.collector.collector import (
    classify_command,
    classify_tool,
    _extract_command,
    _command_family,
    _summarize_path,
    _detect_payload_truncation,
)
from hermes_checker.collector import HookCollector
from hermes_checker.collector.config import CollectorConfig
from hermes_checker.storage import Database, DatabasePaths
import time


# ---------------------------------------------------------------------------
# Command-aware tool classification (Issue 9)
# ---------------------------------------------------------------------------


def test_classify_command_test() -> None:
    assert classify_command("pytest -q") == "test"
    assert classify_command("python -m pytest tests") == "test"
    assert classify_command("npm test --watch") == "test"
    assert classify_command("pnpm test") == "test"
    assert classify_command("yarn test --coverage") == "test"
    assert classify_command("cargo test --release") == "test"
    assert classify_command("go test ./...") == "test"
    assert classify_command("jest --runInBand") == "test"


def test_classify_command_build() -> None:
    assert classify_command("npm run build") == "build"
    assert classify_command("yarn build") == "build"
    assert classify_command("tsc --noEmit") == "build"
    assert classify_command("webpack --mode production") == "build"
    assert classify_command("make all") == "build"
    assert classify_command("cargo build --release") == "build"
    assert classify_command("go build -o app .") == "build"
    assert classify_command("docker build -t myapp .") == "build"


def test_classify_command_git() -> None:
    assert classify_command("git status") == "git"
    assert classify_command("git diff main..feature") == "git"
    assert classify_command("git log --oneline") == "git"
    assert classify_command("git commit -m fix") == "git"
    assert classify_command("/usr/bin/git rev-parse HEAD") == "git"
    assert classify_command("sudo git pull") == "git"


def test_classify_command_lint() -> None:
    assert classify_command("ruff check src/") == "lint"
    assert classify_command("ruff format .") == "lint"
    assert classify_command("mypy src/") == "lint"
    assert classify_command("eslint src/") == "lint"
    assert classify_command("prettier --check .") == "lint"


def test_classify_command_search() -> None:
    assert classify_command("rg 'TODO' src/") == "search"
    assert classify_command("grep -r 'foo' src/") == "search"
    assert classify_command("fd .py src/") == "search"


def test_classify_command_unknown_returns_terminal() -> None:
    assert classify_command("echo hello") == "terminal"
    assert classify_command("ls -la") == "terminal"
    assert classify_command("") == "terminal"
    assert classify_command("not-a-real-command") == "terminal"


def test_classify_tool_with_args() -> None:
    # Generic terminal + a `pytest` command becomes "test"
    assert classify_tool("terminal", {"command": "pytest -q"}) == "test"
    # git command
    assert classify_tool("bash", {"command": "git diff"}) == "git"
    # non-terminal tool name stays at base category
    assert classify_tool("read_file", {"path": "/foo"}) == "file_read"
    # Empty args → no upgrade
    assert classify_tool("terminal", {}) == "terminal"


def test_extract_command_handles_various_shapes() -> None:
    assert _extract_command({"command": "pytest"}) == "pytest"
    assert _extract_command({"cmd": "ls"}) == "ls"
    assert _extract_command({"shell_command": "bash -c 'echo'"}) == "bash -c 'echo'"
    assert _extract_command({"argv": ["echo", "hi"]}) == "echo hi"
    assert _extract_command({"args": "xargs ls"}) == "xargs ls"
    assert _extract_command({"path": "/foo"}) is None
    assert _extract_command({}) is None
    assert _extract_command("not a dict") is None


def test_command_family_aliases() -> None:
    assert _command_family("pnpm install") == "npm"
    assert _command_family("yarn test") == "npm"
    assert _command_family("bun run test") == "npm"
    assert _command_family("uv pip install") == "pip"
    assert _command_family("cargo test") == "cargo"
    assert _command_family("/usr/bin/git") == "git"
    assert _command_family("") == ""


def test_summarize_path_extracts_basename_and_ext() -> None:
    ext, path_hash, basename, stored = _summarize_path(
        {"path": "/home/user/repo/src/foo.py"}
    )
    assert basename == "foo.py"
    assert ext == ".py"
    assert stored == 1
    assert len(path_hash) == 64  # sha256 hex


def test_summarize_path_handles_no_path() -> None:
    assert _summarize_path({}) == ("", "", "", 0)
    assert _summarize_path({"random": "x.py"}) == ("", "", "", 0)  # not in _PATH_KEYS


def test_detect_payload_truncation() -> None:
    assert _detect_payload_truncation({}) is False
    assert _detect_payload_truncation({"_truncated": True}) is True
    assert _detect_payload_truncation({"_truncated": False}) is False
    assert _detect_payload_truncation("not a dict") is False
    assert _detect_payload_truncation({"_truncated": True, "preview": "..."}) is True


# ---------------------------------------------------------------------------
# Integration: payload truncation flags attribution as suppressed
# ---------------------------------------------------------------------------


def test_post_api_request_with_truncated_payload_skips_attribution(
    tmp_path: Path,
) -> None:
    """When Hermes truncated the hook payload, we MUST NOT attribute
    the visible portion as the full prompt."""
    paths = DatabasePaths.from_path(tmp_path / "trunc.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="t1", platform="cli")
    messages = [
        {"role": "system", "content": "fake system message " * 1000},
        {"role": "user", "content": "user question " * 100},
    ]
    coll.pre_api_request(
        session_id="t1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m", messages=messages,
    )
    # Simulate Hermes truncation: response is the sentinel.
    coll.post_api_request(
        session_id="t1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m",
        started_at=0.0, ended_at=1.0, api_duration=1.0,
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 80},
        response={
            "_truncated": True,
            "original_type": "dict",
            "preview": "<omitted>",
        },
    )
    rows = db.api_requests_for_session("t1")
    assert len(rows) == 1
    assert rows[0]["payload_truncated"] == 1
    assert rows[0]["prompt_visible_confidence"] == 0.4
    # No prompt_components were inserted (truncation blocks attribution).
    components = db.prompt_components_for_request(rows[0]["id"])
    assert components == []


def test_post_api_request_with_normal_payload_persists_attribution(
    tmp_path: Path,
) -> None:
    paths = DatabasePaths.from_path(tmp_path / "ok.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="t1", platform="cli")
    coll.pre_api_request(
        session_id="t1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m",
        messages=[
            {"role": "system", "content": "system message " * 100},
            {"role": "user", "content": "user question " * 30},
        ],
    )
    coll.post_api_request(
        session_id="t1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m",
        started_at=0.0, ended_at=1.0, api_duration=1.0,
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 80},
        response={"model": "m", "id": "x"},
    )
    rows = db.api_requests_for_session("t1")
    assert len(rows) == 1
    assert rows[0]["payload_truncated"] == 0
    assert rows[0]["prompt_visible_confidence"] == 1.0
    components = db.prompt_components_for_request(rows[0]["id"])
    assert components, "expected attribution rows"
    # At minimum SYSTEM and USER_MESSAGES are present.
    names = {c["component"] for c in components}
    assert "SYSTEM" in names
    assert "USER_MESSAGES" in names


# ---------------------------------------------------------------------------
# Sanitisation: raw tool args are never persisted (Issue 11)
# ---------------------------------------------------------------------------


def test_post_tool_call_does_not_persist_raw_args(tmp_path: Path) -> None:
    paths = DatabasePaths.from_path(tmp_path / "san.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="s1", platform="cli")
    secret = "sk-aBcDeFgHiJkLmNoPqRsT1234567890"
    coll.pre_tool_call(
        session_id="s1", tool_call_id="t1", tool_name="terminal",
        args={"command": f"echo {secret}"},
    )
    coll.post_tool_call(
        session_id="s1", tool_call_id="t1", tool_name="terminal",
        result="ok", duration_ms=10.0, status="ok",
        started_at=0.0, ended_at=0.01,
    )
    rows = db.tool_calls_for_session("s1")
    assert len(rows) == 1
    r = rows[0]
    # We persist an arg hash + command family + key list, NOT the raw command.
    assert r["args_hash"]
    assert r["command_family"] == "echo"
    # The command_hash is a SHA256 — a one-way function of the command.
    # It does not contain the raw secret in plain text.  We assert the
    # raw command string never appears in any non-hash field.
    import re
    raw = json.dumps(dict(r))
    stripped = re.sub(
        r'"(?:command_hash|args_hash|output_hash|path_hash|request_hash|response_hash)":\s*"[0-9a-f]{64}"',
        "",
        raw,
    )
    assert secret not in stripped, f"secret leaked into {raw}"


# ---------------------------------------------------------------------------
# Sanitisation: tool arg path fields only (Issue 11)
# ---------------------------------------------------------------------------


def test_post_tool_call_records_path_safely(tmp_path: Path) -> None:
    paths = DatabasePaths.from_path(tmp_path / "p.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="p1", platform="cli")
    coll.pre_tool_call(
        session_id="p1", tool_call_id="t1", tool_name="read_file",
        args={"path": "C:\\Users\\xampos\\secret_repo\\src\\component.tsx"},
    )
    coll.post_tool_call(
        session_id="p1", tool_call_id="t1", tool_name="read_file",
        result="<contents>", duration_ms=10.0, status="ok",
        started_at=0.0, ended_at=0.01,
    )
    rows = db.tool_calls_for_session("p1")
    assert len(rows) == 1
    r = rows[0]
    assert r["path_basename"] == "component.tsx"
    assert r["path_ext"] == ".tsx"
    assert r["file_path_stored"] == 1
    # The full path must NOT be in any field we wrote.
    raw = json.dumps(dict(r))
    assert "secret_repo" not in raw
    assert "C:\\Users" not in raw


# ---------------------------------------------------------------------------
# Context delta (Issue 12)
# ---------------------------------------------------------------------------


def test_context_delta_persists_for_consecutive_requests(tmp_path: Path) -> None:
    """Two consecutive API requests in the same session should produce
    a single context_deltas row with the per-component breakdown."""
    paths = DatabasePaths.from_path(tmp_path / "d.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="c1", platform="cli")

    # First request
    coll.pre_api_request(
        session_id="c1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m",
        messages=[
            {"role": "system", "content": "sys " * 100},
            {"role": "user", "content": "q1 " * 50},
        ],
    )
    coll.post_api_request(
        session_id="c1", api_request_id="r1", api_call_count=1,
        provider="openrouter", model="m",
        started_at=0.0, ended_at=1.0, api_duration=1.0,
        usage={"input_tokens": 80, "output_tokens": 20, "cache_read_tokens": 30},
    )
    # Second request
    coll.pre_api_request(
        session_id="c1", api_request_id="r2", api_call_count=2,
        provider="openrouter", model="m",
        turn_id="t1",
        messages=[
            {"role": "system", "content": "sys " * 100},
            {"role": "user", "content": "q1 " * 50},
            {"role": "tool", "content": "result data " * 200},
        ],
    )
    coll.post_api_request(
        session_id="c1", api_request_id="r2", api_call_count=2,
        provider="openrouter", model="m", turn_id="t1",
        started_at=2.0, ended_at=3.0, api_duration=1.0,
        usage={"input_tokens": 280, "output_tokens": 30, "cache_read_tokens": 30},
    )
    deltas = db.context_deltas_for_session("c1")
    assert len(deltas) == 1
    d = deltas[0]
    assert d["provider_delta_tokens"] == 200
    # Coverage should be a positive fraction (we explain SOME of it locally).
    assert d["coverage"] > 0
    assert d["coverage"] <= 1.0
    contributors = json.loads(d["contributors_json"])
    # The biggest delta should be TOOL_RESULTS (the new tool message).
    assert any(c["component"] == "TOOL_RESULTS" and c["tokens"] > 0
               for c in contributors)


# ---------------------------------------------------------------------------
# Self-overhead tracking (Issue 19)
# ---------------------------------------------------------------------------


def test_self_overhead_recording(tmp_path: Path) -> None:
    """_record_self_overhead should INSERT into self_overhead_samples and
    surface a warning when the sample exceeds 50ms (not testable here, but
    we can at least check the row landed)."""
    paths = DatabasePaths.from_path(tmp_path / "self.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="s1", platform="cli")
    # Simulate three calls with different durations.
    for name, dur in (("post_api_request", 12.5), ("pre_tool_call", 7.1),
                     ("post_tool_call", 99.9)):
        coll._record_self_overhead(name, time.time() - dur / 1000.0)
    summary = db.self_overhead_summary()
    assert "post_api_request" in summary
    assert "pre_tool_call" in summary
    assert "post_tool_call" in summary
    # 99.9ms would have triggered a >50ms warning; the row still lands.
    assert summary["post_tool_call"]["max_ms"] >= 99.0
    # P95 is well-defined for a single-sample set.
    assert summary["post_tool_call"]["p95_ms"] >= 99.0


# ---------------------------------------------------------------------------
# Weighted cache hit + fresh tokens (Issue 13)
# ---------------------------------------------------------------------------


def test_session_totals_weighted_cache_hit(tmp_path: Path) -> None:
    paths = DatabasePaths.from_path(tmp_path / "w.db")
    db = Database(paths)
    cfg = CollectorConfig(database_path=str(paths.database))
    coll = HookCollector(database=db, config=cfg)
    coll.on_session_start(session_id="w1", platform="cli")
    # 2 requests: one with full cache, one with no cache
    for api_call_count, cache_read in [(1, 50), (2, 0)]:
        prompt = 100
        coll.pre_api_request(
            session_id="w1", api_request_id=f"r{api_call_count}",
            api_call_count=api_call_count,
            provider="openrouter", model="m",
            messages=[{"role": "user", "content": "x " * 200}],
        )
        coll.post_api_request(
            session_id="w1", api_request_id=f"r{api_call_count}",
            api_call_count=api_call_count,
            provider="openrouter", model="m",
            started_at=0.0, ended_at=1.0, api_duration=1.0,
            usage={"input_tokens": 80, "output_tokens": 20,
                   "cache_read_tokens": cache_read},
        )
    from hermes_checker.reporting import build_report
    report = build_report(db, "w1")
    # The cache_hit is computed as cache_read / prompt_tokens at the
    # request level. prompt_tokens = input + cache_read + cache_write. So
    # for request 1: 80 + 50 + 0 = 130, ratio = 50/130 = 0.3846.
    # For request 2: 80 + 0 + 0 = 80, ratio = 0/80 = 0.
    # The TOKEN-WEIGHTED session ratio is the SUM(cache_read) / SUM(prompt).
    # Sum(prompt) = 130 + 80 = 210.  Sum(cache) = 50. Ratio = 50/210 = 0.238.
    expected = 50 / (130 + 80)
    assert report.totals.cache_hit_ratio_weighted == pytest.approx(expected, rel=1e-3)
    assert report.totals.fresh_tokens == 210 - 50
    assert report.totals.cache_read_tokens == 50
    assert report.totals.cache_write_tokens == 0
