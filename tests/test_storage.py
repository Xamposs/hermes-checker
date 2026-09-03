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


def test_schema_v1_to_v2_migration_preserves_data(tmp_path: Path) -> None:
    """A v1 database must migrate cleanly to v2 without losing rows."""
    import sqlite3
    from hermes_checker.storage.schema import (
        SCHEMA_VERSION,
        apply_migrations,
        _MIGRATIONS,
    )

    # Build a v1-only database by running the v1 migration then committing
    # # schema_version = 1 manually (skip the v2 step).
    conn = sqlite3.connect(str(tmp_path / "v1.db"))
    try:
        v1_sql = next(sql for v, sql in _MIGRATIONS if v == 1)
        conn.executescript(v1_sql)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')"
        )
        # Insert a v1-shape row.
        conn.execute(
            """
            INSERT INTO sessions (session_id, started_at, profile, platform)
            VALUES (?, ?, ?, ?)
            """,
            ("legacy-session", 1.0, "default", "cli"),
        )
        conn.execute(
            """
            INSERT INTO api_requests (
                session_id, started_at, duration_s, prompt_tokens,
                prompt_tokens_provenance, input_tokens,
                input_tokens_provenance, output_tokens, output_tokens_provenance,
                cache_read_tokens, cache_read_tokens_provenance,
                cache_write_tokens, cache_write_tokens_provenance,
                total_tokens, total_tokens_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-session", 1.0, 0.5, 100, "LOCALLY_CALCULATED",
             80, "PROVIDER_MEASURED", 20, "PROVIDER_MEASURED",
             50, "PROVIDER_MEASURED", 0, "PROVIDER_MEASURED",
             120, "LOCALLY_CALCULATED"),
        )
        conn.commit()
    finally:
        conn.close()

    # Now open via Database() which should apply pending v2.
    paths = DatabasePaths.from_path(tmp_path / "v1.db")
    db2 = Database(paths)
    try:
        assert db2.schema_version == SCHEMA_VERSION
        # The v1 row must still be queryable.
        row = db2.session("legacy-session")
        assert row is not None
        assert row["profile"] == "default"
        # v1 api_requests must still be readable through the same query
        # path, with NULLs for the v2-only columns.
        reqs = db2.api_requests_for_session("legacy-session")
        assert len(reqs) == 1
        assert reqs[0]["prompt_tokens"] == 100
        assert reqs[0]["payload_truncated"] is None  # new v2 column
        # v2 tables must exist.
        tables = {
            r[0]
            for r in db2._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for required in (
            "static_prompt_snapshots", "static_skill_breakdowns",
            "static_toolset_breakdowns", "skill_events",
            "context_deltas", "app_config", "self_overhead_samples",
        ):
            assert required in tables, f"v2 table missing: {required}"
    finally:
        db2.close()


def test_context_delta_round_trip(db: Database) -> None:
    db.upsert_session(session_id="d1", started_at=1.0)
    rid_a = db.insert_api_request({
        "session_id": "d1", "started_at": 1.0, "duration_s": 0.5,
        "prompt_tokens": 100, "prompt_tokens_provenance": "LOCALLY_CALCULATED",
        "input_tokens": 80, "input_tokens_provenance": "PROVIDER_MEASURED",
        "output_tokens": 20, "output_tokens_provenance": "PROVIDER_MEASURED",
        "cache_read_tokens": 0, "cache_read_tokens_provenance": "UNAVAILABLE",
        "cache_write_tokens": 0, "cache_write_tokens_provenance": "UNAVAILABLE",
        "total_tokens": 100, "total_tokens_provenance": "LOCALLY_CALCULATED",
    })
    rid_b = db.insert_api_request({
        "session_id": "d1", "started_at": 2.0, "duration_s": 0.5,
        "prompt_tokens": 130, "prompt_tokens_provenance": "LOCALLY_CALCULATED",
        "input_tokens": 100, "input_tokens_provenance": "PROVIDER_MEASURED",
        "output_tokens": 30, "output_tokens_provenance": "PROVIDER_MEASURED",
        "cache_read_tokens": 0, "cache_read_tokens_provenance": "UNAVAILABLE",
        "cache_write_tokens": 0, "cache_write_tokens_provenance": "UNAVAILABLE",
        "total_tokens": 130, "total_tokens_provenance": "LOCALLY_CALCULATED",
    })
    db.insert_context_delta({
        "session_id": "d1",
        "previous_api_request_id": rid_a,
        "current_api_request_id": rid_b,
        "provider_delta_tokens": 30,
        "explained_tokens": 25,
        "unexplained_tokens": 5,
        "coverage": 25/30,
        "contributors": [{"component": "TOOL_RESULTS", "tokens": 25}],
        "confidence": 0.85,
    })
    deltas = db.context_deltas_for_session("d1")
    assert len(deltas) == 1
    assert deltas[0]["provider_delta_tokens"] == 30
    assert deltas[0]["coverage"] == pytest.approx(25/30, rel=1e-3)
    import json
    contributors = json.loads(deltas[0]["contributors_json"])
    assert contributors[0]["component"] == "TOOL_RESULTS"


def test_tool_call_api_request_correlation(db: Database) -> None:
    db.upsert_session(session_id="c1", started_at=1.0)
    rid = db.insert_api_request({
        "session_id": "c1", "started_at": 1.0, "duration_s": 0.5,
        "prompt_tokens": 100, "prompt_tokens_provenance": "LOCALLY_CALCULATED",
        "input_tokens": 80, "input_tokens_provenance": "PROVIDER_MEASURED",
        "output_tokens": 20, "output_tokens_provenance": "PROVIDER_MEASURED",
        "cache_read_tokens": 0, "cache_read_tokens_provenance": "UNAVAILABLE",
        "cache_write_tokens": 0, "cache_write_tokens_provenance": "UNAVAILABLE",
        "total_tokens": 100, "total_tokens_provenance": "LOCALLY_CALCULATED",
    })
    tcid = db.insert_tool_call({
        "session_id": "c1",
        "tool_name": "terminal", "category": "terminal",
        "started_at": 2.0, "ended_at": 2.5, "duration_ms": 500.0,
        "status": "ok", "output_chars": 100, "output_tokens_est": 25,
        "output_hash": "abc",
    })
    # Backfill correlation
    db.update_tool_call_api_request(tcid, rid)
    rows = db.tool_calls_for_session("c1")
    assert rows[0]["api_request_row_id"] == rid


def test_v2_only_round_trip(db: Database) -> None:
    """All v2 fields should round-trip correctly on a fresh database."""
    db.upsert_session(session_id="s2", started_at=1.0)
    rid = db.insert_api_request({
        "session_id": "s2",
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
        # V1.1 fields
        "prompt_visible_chars": 5000,
        "prompt_visible_provenance": "PROVIDER_MEASURED",
        "prompt_visible_tokens_est": 1200,
        "prompt_visible_confidence": 1.0,
        "payload_truncated": 0,
        "weight_cached": 0,
        "weight_prompt": 100,
    })
    rows = db.api_requests_for_session("s2")
    assert len(rows) == 1
    r = rows[0]
    assert r["prompt_visible_chars"] == 5000
    assert r["prompt_visible_provenance"] == "PROVIDER_MEASURED"
    assert r["payload_truncated"] == 0
    assert r["weight_prompt"] == 100


def test_v2_tables_round_trip(db: Database) -> None:
    """Static snapshots, skill events, deltas, app config, self-overhead."""
    db.upsert_session(session_id="s3", started_at=1.0)
    sid = db.insert_static_snapshot(
        {
            "taken_at": 1.0,
            "hermes_version": "40.10.2",
            "platform": "cli",
            "model": "MiniMax-m3",
            "base_url": "https://example/v1",
            "system_prompt_chars": 4500,
            "system_prompt_bytes": 4500,
            "system_prompt_tokens_est": 1100,
            "stable_chars": 2000,
            "stable_bytes": 2000,
            "stable_tokens_est": 500,
            "context_chars": 1000,
            "context_bytes": 1000,
            "context_tokens_est": 250,
            "volatile_chars": 1500,
            "volatile_bytes": 1500,
            "volatile_tokens_est": 350,
            "skills_index_chars": 800,
            "skills_index_bytes": 800,
            "skills_index_tokens_est": 200,
            "memory_chars": 200,
            "memory_bytes": 200,
            "memory_tokens_est": 50,
            "user_profile_chars": 100,
            "user_profile_bytes": 100,
            "user_profile_tokens_est": 25,
            "tools_count": 42,
            "tools_json_bytes": 12000,
            "tools_json_tokens_est": 3000,
            "mcp_schemas_chars": 0,
            "mcp_schemas_bytes": 0,
            "mcp_schemas_tokens_est": 0,
            "subagent_defs_chars": 0,
            "subagent_defs_bytes": 0,
            "subagent_defs_tokens_est": 0,
            "other_chars": 0,
            "other_bytes": 0,
            "other_tokens_est": 0,
            "tokenizer_method": "HEURISTIC",
            "hermes_native": 1,
            "metadata_json": '{"hermes_compute_prompt_breakdown": "ok"}',
        },
        skills=[
            {"skill_name": "alpha", "index_line_chars": 200,
             "index_line_bytes": 200, "index_line_tokens_est": 50,
             "rank_in_index": 0},
        ],
        toolsets=[
            {"toolset_name": "hermes-cli", "tool_count": 10,
             "schema_chars": 3000, "schema_bytes": 3000, "schema_tokens_est": 750},
        ],
    )
    assert sid > 0
    snap = db.latest_snapshot(model="MiniMax-m3")
    assert snap is not None
    assert snap["hermes_native"] == 1
    assert snap["system_prompt_tokens_est"] == 1100
    skills = db.snapshot_skills(sid)
    assert len(skills) == 1 and skills[0]["skill_name"] == "alpha"
    toolsets = db.snapshot_toolsets(sid)
    assert len(toolsets) == 1 and toolsets[0]["toolset_name"] == "hermes-cli"

    # Skill event
    db.insert_skill_event(
        session_id="s3", task_id="t1", turn_id="turn-1",
        skill_name="alpha", action="loaded",
        use_count=3, reused=True, reuse_after_patch=False,
    )
    sevs = db.skill_events_for_session("s3")
    assert len(sevs) == 1
    assert sevs[0]["skill_name"] == "alpha"
    assert sevs[0]["reused"] == 1

    # App config
    db.set_app_config("experiment", "baseline-minimax-direct")
    assert db.get_app_config("experiment") == "baseline-minimax-direct"
    assert db.get_app_config("nonexistent") is None

    # Self overhead
    for i in range(10):
        db.record_self_overhead("post_api_request", 1.5 + i * 0.1)
    summary = db.self_overhead_summary("post_api_request")
    assert "post_api_request" in summary
    assert summary["post_api_request"]["count"] == 10
    assert summary["post_api_request"]["max_ms"] >= 2.0


def test_schema_v1_to_v2_to_v3_migration_preserves_data(tmp_path: Path) -> None:
    """A v1 database must migrate cleanly to v3 without losing rows."""
    import sqlite3
    from hermes_checker.storage.schema import (
        SCHEMA_VERSION,
        apply_migrations,
        _MIGRATIONS,
    )

    conn = sqlite3.connect(str(tmp_path / "v1.db"))
    try:
        v1_sql = next(sql for v, sql in _MIGRATIONS if v == 1)
        conn.executescript(v1_sql)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')"
        )
        # Insert a v1-shape row.
        conn.execute(
            """
            INSERT INTO sessions (session_id, started_at, profile, platform)
            VALUES (?, ?, ?, ?)
            """,
            ("legacy-session", 1.0, "default", "cli"),
        )
        conn.execute(
            """
            INSERT INTO api_requests (
                session_id, started_at, prompt_tokens, prompt_tokens_provenance,
                input_tokens, input_tokens_provenance,
                output_tokens, output_tokens_provenance,
                cache_read_tokens, cache_read_tokens_provenance,
                cache_write_tokens, cache_write_tokens_provenance,
                total_tokens, total_tokens_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-session", 1.0, 100, "LOCALLY_CALCULATED",
             80, "PROVIDER_MEASURED", 20, "PROVIDER_MEASURED",
             50, "PROVIDER_MEASURED", 0, "PROVIDER_MEASURED",
             120, "LOCALLY_CALCULATED"),
        )
        api_row_id = conn.execute(
            "SELECT id FROM api_requests WHERE session_id=?",
            ("legacy-session",),
        ).fetchone()[0]
        # Insert v1 prompt_components row (no provenance column at v1).
        cur = conn.execute(
            """
            INSERT INTO prompt_components (
                api_request_row_id, component, characters, bytes,
                estimated_tokens, measurement_method, confidence
            ) VALUES (?, 'SYSTEM', 100, 100, 25, 'HEURISTIC', 0.9)
            """,
            (api_row_id,),
        )
        legacy_row_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # Now open via Database() which should apply pending v2 + v3.
    paths = DatabasePaths.from_path(tmp_path / "v1.db")
    db2 = Database(paths)
    try:
        assert db2.schema_version == SCHEMA_VERSION
        # The v1 row must still be queryable, with v3-backfilled provenance.
        rows = db2.prompt_components_for_request(api_row_id)
        assert any(r["component"] == "SYSTEM" for r in rows)
        # The legacy row should have provenance='LOCALLY_ESTIMATED' (v3 backfill).
        legacy = next(r for r in rows if r["id"] == legacy_row_id)
        assert "provenance" in legacy.keys()
        assert legacy["provenance"] == "LOCALLY_ESTIMATED"
        # v2 tables must exist.
        tables = {
            r[0]
            for r in db2._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for required in (
            "static_prompt_snapshots", "static_skill_breakdowns",
            "static_toolset_breakdowns", "skill_events",
            "context_deltas", "app_config", "self_overhead_samples",
        ):
            assert required in tables, f"v2 table missing: {required}"
    finally:
        db2.close()