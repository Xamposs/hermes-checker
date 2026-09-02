"""Smoke tests for the SQLite schema migrations and the Database helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_checker.storage import Database, DatabasePaths
from hermes_checker.storage.schema import (
    SCHEMA_VERSION,
    apply_migrations,
    iter_migration_ids,
)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    paths = DatabasePaths.from_path(tmp_path / "hc.db")
    return Database(paths)


def test_apply_migrations_creates_all_tables(db: Database) -> None:
    tables = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected = {
        "schema_meta",
        "sessions",
        "turns",
        "models",
        "providers",
        "api_requests",
        "prompt_components",
        "tool_calls",
        "pricing_profiles",
        "events",
        "optimizer_findings",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_schema_version_is_bumped(db: Database) -> None:
    assert db.schema_version == max(iter_migration_ids())
    assert db.schema_version == SCHEMA_VERSION


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    try:
        v1 = apply_migrations(conn)
        v2 = apply_migrations(conn)
        assert v1 == v2 == SCHEMA_VERSION
    finally:
        conn.close()


def test_upsert_session_round_trip(db: Database) -> None:
    sid = "sess-1"
    db.upsert_session(
        session_id=sid,
        profile="default",
        platform="desktop",
        started_at=1.0,
        experiment="baseline-minimax-direct",
    )
    row = db.session(sid)
    assert row is not None
    assert row["session_id"] == sid
    assert row["profile"] == "default"
    assert row["platform"] == "desktop"
    assert row["experiment"] == "baseline-minimax-direct"

    # Re-upsert with ended_at
    db.upsert_session(session_id=sid, ended_at=42.0, started_at=1.0)
    row2 = db.session(sid)
    assert row2["ended_at"] == 42.0
    assert row2["profile"] == "default"  # preserved


def test_remember_provider_and_model(db: Database) -> None:
    db.remember_provider("opencode", base_url="https://x.example/v1")
    db.remember_model("opencode", "MiniMax-m3", metadata={"family": "minimax"})
    db.remember_model("opencode", "deepseek-v4-flash")

    rows = list(db._conn.execute("SELECT * FROM providers"))
    assert any(r["provider"] == "opencode" for r in rows)
    model_rows = list(db._conn.execute("SELECT * FROM models"))
    names = sorted(r["model"] for r in model_rows)
    assert names == ["MiniMax-m3", "deepseek-v4-flash"]


def test_insert_api_request_persists_provenance(db: Database) -> None:
    db.upsert_session(session_id="s", started_at=1.0)
    row_id = db.insert_api_request({
        "session_id": "s",
        "provider": "opencode",
        "model": "MiniMax-m3",
        "started_at": 1.0,
        "ended_at": 2.0,
        "duration_s": 1.0,
        "prompt_tokens": 100,
        "prompt_tokens_provenance": "LOCALLY_CALCULATED",
        "input_tokens": 80,
        "input_tokens_provenance": "PROVIDER_MEASURED",
        "output_tokens": 20,
        "output_tokens_provenance": "PROVIDER_MEASURED",
        "cache_read_tokens": 0,
        "cache_read_tokens_provenance": "UNAVAILABLE",
        "cache_write_tokens": 0,
        "cache_write_tokens_provenance": "UNAVAILABLE",
        "total_tokens": 100,
        "total_tokens_provenance": "LOCALLY_CALCULATED",
        "tokens_per_second": 20.0,
        "cache_hit_ratio": 0.0,
        "messages_count": 12,
    })
    assert row_id > 0
    rows = db.api_requests_for_session("s")
    assert len(rows) == 1
    r = rows[0]
    assert r["prompt_tokens"] == 100
    assert r["input_tokens_provenance"] == "PROVIDER_MEASURED"
    assert r["output_tokens_provenance"] == "PROVIDER_MEASURED"


def test_insert_tool_call(db: Database) -> None:
    db.upsert_session(session_id="s", started_at=1.0)
    db.insert_tool_call({
        "session_id": "s",
        "tool_name": "terminal",
        "category": "terminal",
        "started_at": 1.0,
        "ended_at": 1.5,
        "duration_ms": 500.0,
        "status": "ok",
        "output_chars": 1234,
        "output_tokens_est": 308,
        "output_hash": "deadbeef",
        "args_summary": "pytest",
    })
    rows = db.tool_calls_for_session("s")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "terminal"
    assert rows[0]["category"] == "terminal"


def test_prompt_components(db: Database) -> None:
    db.upsert_session(session_id="s", started_at=1.0)
    rid = db.insert_api_request({
        "session_id": "s",
        "started_at": 1.0,
        "duration_s": 1.0,
    })
    db.insert_prompt_components(rid, [
        {"component": "SYSTEM", "characters": 100, "bytes": 100, "estimated_tokens": 25, "measurement_method": "TIKTOKEN", "confidence": 0.9},
        {"component": "TOOLS_SCHEMA", "characters": 200, "bytes": 200, "estimated_tokens": 50, "measurement_method": "TIKTOKEN", "confidence": 0.9},
    ])
    rows = db.prompt_components_for_request(rid)
    assert {r["component"] for r in rows} == {"SYSTEM", "TOOLS_SCHEMA"}


def test_pricing_round_trip(db: Database) -> None:
    db.upsert_pricing(
        "openrouter-2026-09",
        {
            "model": "deepseek/deepseek-chat-v4-flash",
            "input_per_million_usd": 0.27,
            "cached_input_per_million_usd": 0.07,
            "output_per_million_usd": 1.10,
            "notes": "test",
        },
    )
    rows = db.pricing("openrouter-2026-09")
    assert len(rows) == 1
    assert rows[0]["input_per_million_usd"] == 0.27


def test_insert_event_and_finding(db: Database) -> None:
    db.upsert_session(session_id="s", started_at=1.0)
    eid = db.insert_event(session_id="s", event_type="session_end", payload={"k": 1})
    assert eid > 0
    fid = db.insert_finding(
        session_id="s",
        finding_kind="large_context_jump",
        severity="POTENTIAL_WASTE",
        confidence=0.8,
        message="+30k tokens",
        evidence={"delta_tokens": 30_000},
    )
    assert fid > 0
    findings = db.findings(session_id="s")
    assert findings and findings[0]["finding_kind"] == "large_context_jump"