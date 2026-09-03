"""SQLite-backed persistence for Hermes Checker.

The :class:`Database` class is a thin wrapper around ``sqlite3.Connection``
that applies migrations on open and exposes typed writer methods for the
data the collector emits.

Design notes
------------

- All public write methods commit individually. The collector writes one
  row per Hermes event; coalescing would force the dashboard to query
  intermediate state and is not worth the complexity in V1.1.
- WAL mode is enabled so the dashboard can read concurrently while the
  collector writes.
- Foreign keys are enforced; the schema relies on this.
- Provenance tags are stored as text columns so the dashboard can
  surface them. The :class:`Database` itself never inspects or
  overwrites a value's provenance label — the caller decides.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .schema import SCHEMA_VERSION, initialise_database


@dataclass(frozen=True)
class DatabasePaths:
    """Standard locations for Hermes Checker's persisted state.

    These follow the same ``%USERPROFILE%\\.hermes-checker\\`` convention
    the spec calls out.  We never reuse Hermes' own directory so the two
    systems can't accidentally trample each other.
    """

    root: Path
    database: Path

    @classmethod
    def default(cls) -> "DatabasePaths":
        root = Path(os.environ.get("HERMES_CHECKER_HOME")
                    or (Path.home() / ".hermes-checker"))
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root, database=root / "hermes-checker.db")

    @classmethod
    def from_path(cls, database: Path) -> "DatabasePaths":
        database.parent.mkdir(parents=True, exist_ok=True)
        return cls(root=database.parent, database=database)


class Database:
    """Single-process wrapper around a SQLite file.

    Thread-safe via an internal write lock; reads are unlocked because
    sqlite3 connections are not safe to share across threads without
    ``check_same_thread=False`` and even then we keep it simple.  The
    dashboard opens its own read-only connection.
    """

    def __init__(self, paths: DatabasePaths, *, readonly: bool = False) -> None:
        self.paths = paths
        self.readonly = readonly
        self._conn = sqlite3.connect(
            str(paths.database),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions ourselves
        )
        self._conn.row_factory = sqlite3.Row
        if not readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            initialise_database(self._conn)
        else:
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.Lock()

    @property
    def schema_version(self) -> int:
        cur = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        *,
        session_id: str,
        profile: Optional[str] = None,
        platform: Optional[str] = None,
        started_at: float,
        ended_at: Optional[float] = None,
        experiment: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        """Insert or update a session row. Returns the row id."""
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO sessions (session_id, profile, platform, started_at, ended_at,
                                      experiment, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    profile=COALESCE(excluded.profile, sessions.profile),
                    platform=COALESCE(excluded.platform, sessions.platform),
                    ended_at=COALESCE(excluded.ended_at, sessions.ended_at),
                    experiment=COALESCE(excluded.experiment, sessions.experiment),
                    metadata_json=COALESCE(excluded.metadata_json, sessions.metadata_json)
                """,
                (
                    session_id,
                    profile,
                    platform,
                    started_at,
                    ended_at,
                    experiment,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def session_row_id(self, session_id: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------
    # Turns (best-effort)
    # ------------------------------------------------------------------

    def upsert_turn(
        self,
        *,
        session_id: str,
        turn_id: Optional[str],
        started_at: float,
        ended_at: Optional[float] = None,
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO turns (session_id, turn_id, started_at, ended_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, turn_id, started_at, ended_at),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Providers / Models
    # ------------------------------------------------------------------

    def remember_provider(self, provider: str, base_url: Optional[str] = None,
                          at: Optional[float] = None) -> None:
        if not provider:
            return
        at = at or time.time()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO providers (provider, base_url, first_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    base_url=COALESCE(excluded.base_url, providers.base_url)
                """,
                (provider, base_url, at),
            )
            self._conn.commit()

    def remember_model(self, provider: str, model: str,
                       metadata: Optional[dict[str, Any]] = None,
                       at: Optional[float] = None) -> None:
        if not provider or not model:
            return
        at = at or time.time()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO models (provider, model, first_seen, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    metadata_json=COALESCE(excluded.metadata_json, models.metadata_json)
                """,
                (provider, model, at,
                 json.dumps(metadata) if metadata else None),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # API requests (the heart of the system)
    # ------------------------------------------------------------------

    def insert_api_request(self, row: dict[str, Any]) -> int:
        """Insert an api_requests row from a prepared dict.

        The dict MUST contain every column we plan to write. The caller
        (the collector) is responsible for computing derived columns
        (duration_s, ttft_s, tokens_per_second, cache_hit_ratio,
        context_tier_snapshot_id) and for picking the right provenance
        label per bucket.
        """
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO api_requests (
                    session_id, turn_id, api_request_id, api_call_count,
                    provider, model, base_url, api_mode, streaming,
                    started_at, ended_at, first_chunk_at, duration_s, ttft_s,
                    finish_reason, response_model, messages_count,
                    prompt_tokens, prompt_tokens_provenance,
                    input_tokens, input_tokens_provenance,
                    output_tokens, output_tokens_provenance,
                    reasoning_tokens, reasoning_tokens_provenance,
                    cache_read_tokens, cache_read_tokens_provenance,
                    cache_write_tokens, cache_write_tokens_provenance,
                    total_tokens, total_tokens_provenance,
                    tokens_per_second, cache_hit_ratio,
                    raw_usage_json,
                    assistant_content_chars, assistant_tool_call_count,
                    request_hash, response_hash,
                    prompt_visible_chars, prompt_visible_provenance,
                    prompt_visible_tokens_est, prompt_visible_confidence,
                    payload_truncated, weight_cached, weight_prompt,
                    context_tier_snapshot_id,
                    metadata_json
                ) VALUES (
                    :session_id, :turn_id, :api_request_id, :api_call_count,
                    :provider, :model, :base_url, :api_mode, :streaming,
                    :started_at, :ended_at, :first_chunk_at, :duration_s, :ttft_s,
                    :finish_reason, :response_model, :messages_count,
                    :prompt_tokens, :prompt_tokens_provenance,
                    :input_tokens, :input_tokens_provenance,
                    :output_tokens, :output_tokens_provenance,
                    :reasoning_tokens, :reasoning_tokens_provenance,
                    :cache_read_tokens, :cache_read_tokens_provenance,
                    :cache_write_tokens, :cache_write_tokens_provenance,
                    :total_tokens, :total_tokens_provenance,
                    :tokens_per_second, :cache_hit_ratio,
                    :raw_usage_json,
                    :assistant_content_chars, :assistant_tool_call_count,
                    :request_hash, :response_hash,
                    :prompt_visible_chars, :prompt_visible_provenance,
                    :prompt_visible_tokens_est, :prompt_visible_confidence,
                    :payload_truncated, :weight_cached, :weight_prompt,
                    :context_tier_snapshot_id,
                    :metadata_json
                )
                """,
                _api_request_bind(row),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Prompt component attribution
    # ------------------------------------------------------------------

    def insert_prompt_components(self, api_request_row_id: int,
                                 components: Iterable[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        with self._write_lock:
            for c in components:
                cur = self._conn.execute(
                    """
                    INSERT INTO prompt_components (
                        api_request_row_id, component, characters, bytes,
                        estimated_tokens, measurement_method, confidence,
                        source_identifier, provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        api_request_row_id,
                        c["component"],
                        int(c["characters"]),
                        int(c["bytes"]),
                        int(c["estimated_tokens"]),
                        c["measurement_method"],
                        float(c["confidence"]),
                        c.get("source_identifier"),
                        c.get("provenance", "LOCALLY_ESTIMATED"),
                    ),
                )
                ids.append(cur.lastrowid or 0)
            self._conn.commit()
        return ids

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def insert_tool_call(self, row: dict[str, Any]) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO tool_calls (
                    session_id, turn_id, api_request_row_id,
                    tool_call_id, tool_name, category,
                    started_at, ended_at, duration_ms,
                    status, error_type, error_message, exit_code,
                    output_truncated,
                    input_chars, input_tokens_est,
                    output_chars, output_tokens_est,
                    output_hash, args_hash, args_summary, metadata_json,
                    command_family, command_hash,
                    input_measurement_method, output_measurement_method,
                    input_tokens, args_keys_json,
                    path_ext, path_hash, path_basename, file_path_stored
                ) VALUES (
                    :session_id, :turn_id, :api_request_row_id,
                    :tool_call_id, :tool_name, :category,
                    :started_at, :ended_at, :duration_ms,
                    :status, :error_type, :error_message, :exit_code,
                    :output_truncated,
                    :input_chars, :input_tokens_est,
                    :output_chars, :output_tokens_est,
                    :output_hash, :args_hash, :args_summary, :metadata_json,
                    :command_family, :command_hash,
                    :input_measurement_method, :output_measurement_method,
                    :input_tokens, :args_keys_json,
                    :path_ext, :path_hash, :path_basename, :file_path_stored
                )
                """,
                _tool_call_bind(row),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def update_tool_call_api_request(self, tool_call_row_id: int,
                                     api_request_row_id: int) -> None:
        """Backfill the api_request_row_id of a tool call we correlated later."""
        with self._write_lock:
            self._conn.execute(
                "UPDATE tool_calls SET api_request_row_id=? WHERE id=?",
                (api_request_row_id, tool_call_row_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def insert_event(
        self,
        *,
        session_id: Optional[str],
        event_type: str,
        event_at: Optional[float] = None,
        api_request_row_id: Optional[int] = None,
        tool_call_row_id: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (session_id, api_request_row_id, tool_call_row_id,
                                    event_type, event_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    api_request_row_id,
                    tool_call_row_id,
                    event_type,
                    event_at or time.time(),
                    json.dumps(payload) if payload else None,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def insert_finding(
        self,
        *,
        session_id: Optional[str],
        finding_kind: str,
        severity: str,
        confidence: float,
        detected_at: Optional[float] = None,
        evidence: Optional[dict[str, Any]] = None,
        message: str = "",
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO optimizer_findings (
                    session_id, finding_kind, severity, confidence,
                    detected_at, evidence_json, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    finding_kind,
                    severity,
                    float(confidence),
                    detected_at or time.time(),
                    json.dumps(evidence) if evidence else None,
                    message,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Static prompt snapshots (V1.1)
    # ------------------------------------------------------------------

    def insert_static_snapshot(
        self,
        row: dict[str, Any],
        *,
        skills: Iterable[dict[str, Any]] = (),
        toolsets: Iterable[dict[str, Any]] = (),
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO static_prompt_snapshots (
                    taken_at, hermes_version, platform, model, base_url,
                    system_prompt_chars, system_prompt_bytes, system_prompt_tokens_est,
                    stable_chars, stable_bytes, stable_tokens_est,
                    context_chars, context_bytes, context_tokens_est,
                    volatile_chars, volatile_bytes, volatile_tokens_est,
                    skills_index_chars, skills_index_bytes, skills_index_tokens_est,
                    memory_chars, memory_bytes, memory_tokens_est,
                    user_profile_chars, user_profile_bytes, user_profile_tokens_est,
                    tools_count, tools_json_bytes, tools_json_tokens_est,
                    mcp_schemas_chars, mcp_schemas_bytes, mcp_schemas_tokens_est,
                    subagent_defs_chars, subagent_defs_bytes, subagent_defs_tokens_est,
                    other_chars, other_bytes, other_tokens_est,
                    tokenizer_method, hermes_native, metadata_json
                ) VALUES (
                    :taken_at, :hermes_version, :platform, :model, :base_url,
                    :system_prompt_chars, :system_prompt_bytes, :system_prompt_tokens_est,
                    :stable_chars, :stable_bytes, :stable_tokens_est,
                    :context_chars, :context_bytes, :context_tokens_est,
                    :volatile_chars, :volatile_bytes, :volatile_tokens_est,
                    :skills_index_chars, :skills_index_bytes, :skills_index_tokens_est,
                    :memory_chars, :memory_bytes, :memory_tokens_est,
                    :user_profile_chars, :user_profile_bytes, :user_profile_tokens_est,
                    :tools_count, :tools_json_bytes, :tools_json_tokens_est,
                    :mcp_schemas_chars, :mcp_schemas_bytes, :mcp_schemas_tokens_est,
                    :subagent_defs_chars, :subagent_defs_bytes, :subagent_defs_tokens_est,
                    :other_chars, :other_bytes, :other_tokens_est,
                    :tokenizer_method, :hermes_native, :metadata_json
                )
                """,
                _snapshot_bind(row),
            )
            sid = cur.lastrowid or 0
            for s in skills:
                self._conn.execute(
                    """
                    INSERT INTO static_skill_breakdowns (
                        snapshot_id, skill_name, index_line_chars, index_line_bytes,
                        index_line_tokens_est, skill_md_chars, skill_md_bytes,
                        skill_md_tokens_est, rank_in_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        s["skill_name"],
                        s.get("index_line_chars"),
                        s.get("index_line_bytes"),
                        s.get("index_line_tokens_est"),
                        s.get("skill_md_chars"),
                        s.get("skill_md_bytes"),
                        s.get("skill_md_tokens_est"),
                        s.get("rank_in_index"),
                    ),
                )
            for t in toolsets:
                self._conn.execute(
                    """
                    INSERT INTO static_toolset_breakdowns (
                        snapshot_id, toolset_name, tool_count, schema_chars,
                        schema_bytes, schema_tokens_est
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        t["toolset_name"],
                        t.get("tool_count"),
                        t.get("schema_chars"),
                        t.get("schema_bytes"),
                        t.get("schema_tokens_est"),
                    ),
                )
            self._conn.commit()
            return sid

    def latest_snapshot(self, model: Optional[str] = None) -> Optional[sqlite3.Row]:
        if model:
            return self._conn.execute(
                "SELECT * FROM static_prompt_snapshots WHERE model=? "
                "ORDER BY taken_at DESC LIMIT 1",
                (model,),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM static_prompt_snapshots ORDER BY taken_at DESC LIMIT 1"
        ).fetchone()

    def snapshot_skills(self, snapshot_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM static_skill_breakdowns WHERE snapshot_id=? "
                "ORDER BY rank_in_index IS NULL, rank_in_index",
                (snapshot_id,),
            )
        )

    def snapshot_toolsets(self, snapshot_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM static_toolset_breakdowns WHERE snapshot_id=?",
                (snapshot_id,),
            )
        )

    # ------------------------------------------------------------------
    # Skill events
    # ------------------------------------------------------------------

    def insert_skill_event(
        self,
        *,
        session_id: Optional[str],
        task_id: Optional[str],
        turn_id: Optional[str],
        skill_name: str,
        action: str,
        detected_at: Optional[float] = None,
        use_count: Optional[int] = None,
        reused: Optional[bool] = None,
        reuse_after_patch: Optional[bool] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO skill_events (
                    session_id, task_id, turn_id, skill_name, action,
                    detected_at, use_count, reused, reuse_after_patch,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_id,
                    turn_id,
                    skill_name,
                    action,
                    detected_at or time.time(),
                    use_count,
                    1 if reused else 0 if reused is not None else None,
                    1 if reuse_after_patch else 0 if reuse_after_patch is not None else None,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def skill_events_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM skill_events WHERE session_id=? "
                "ORDER BY detected_at ASC",
                (session_id,),
            )
        )

    # ------------------------------------------------------------------
    # Context deltas
    # ------------------------------------------------------------------

    def insert_context_delta(self, row: dict[str, Any]) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO context_deltas (
                    session_id, previous_api_request_id, current_api_request_id,
                    provider_delta_tokens, explained_tokens, unexplained_tokens,
                    coverage, contributors_json, detected_at, confidence,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["session_id"],
                    row.get("previous_api_request_id"),
                    row["current_api_request_id"],
                    row.get("provider_delta_tokens"),
                    row.get("explained_tokens"),
                    row.get("unexplained_tokens"),
                    row.get("coverage"),
                    json.dumps(row.get("contributors"))
                    if row.get("contributors") else None,
                    row.get("detected_at") or time.time(),
                    row.get("confidence"),
                    json.dumps(row.get("metadata")) if row.get("metadata") else None,
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def context_deltas_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM context_deltas WHERE session_id=? "
                "ORDER BY detected_at ASC",
                (session_id,),
            )
        )

    # ------------------------------------------------------------------
    # App config
    # ------------------------------------------------------------------

    def get_app_config(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM app_config WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_app_config(self, key: str, value: str) -> None:
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO app_config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, time.time()),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Self-overhead samples
    # ------------------------------------------------------------------

    def record_self_overhead(self, callback_name: str, duration_ms: float) -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO self_overhead_samples (callback_name, duration_ms, sampled_at) "
                "VALUES (?, ?, ?)",
                (callback_name, duration_ms, time.time()),
            )
            self._conn.commit()

    def self_overhead_summary(self, callback_name: Optional[str] = None,
                              limit: int = 2000) -> dict[str, dict[str, float]]:
        if callback_name:
            rows = self._conn.execute(
                "SELECT * FROM self_overhead_samples WHERE callback_name=? "
                "ORDER BY sampled_at DESC LIMIT ?",
                (callback_name, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM self_overhead_samples ORDER BY sampled_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: dict[str, list[float]] = {}
        for r in rows:
            out.setdefault(r["callback_name"], []).append(float(r["duration_ms"]))
        summary: dict[str, dict[str, float]] = {}
        for name, samples in out.items():
            samples.sort()
            summary[name] = {
                "count": len(samples),
                "avg_ms": sum(samples) / max(1, len(samples)),
                "max_ms": samples[-1],
                "p50_ms": _percentile(samples, 50.0),
                "p95_ms": _percentile(samples, 95.0),
            }
        return summary

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def upsert_pricing(self, profile_name: str, entry: dict[str, Any]) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO pricing_profiles (
                    profile_name, model,
                    input_per_million_usd, cached_input_per_million_usd,
                    cache_write_per_million_usd, output_per_million_usd,
                    reasoning_per_million_usd, request_cost_usd, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_name) DO UPDATE SET
                    model=excluded.model,
                    input_per_million_usd=excluded.input_per_million_usd,
                    cached_input_per_million_usd=excluded.cached_input_per_million_usd,
                    cache_write_per_million_usd=excluded.cache_write_per_million_usd,
                    output_per_million_usd=excluded.output_per_million_usd,
                    reasoning_per_million_usd=excluded.reasoning_per_million_usd,
                    request_cost_usd=excluded.request_cost_usd,
                    notes=excluded.notes
                """,
                (
                    profile_name,
                    entry["model"],
                    entry.get("input_per_million_usd"),
                    entry.get("cached_input_per_million_usd"),
                    entry.get("cache_write_per_million_usd"),
                    entry.get("output_per_million_usd"),
                    entry.get("reasoning_per_million_usd"),
                    entry.get("request_cost_usd"),
                    entry.get("notes"),
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Query helpers used by the dashboard / CLI
    # ------------------------------------------------------------------

    def sessions(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def session(self, session_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()

    def api_requests_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM api_requests
                WHERE session_id=?
                ORDER BY started_at ASC
                """,
                (session_id,),
            )
        )

    def tool_calls_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM tool_calls
                WHERE session_id=?
                ORDER BY started_at ASC
                """,
                (session_id,),
            )
        )

    def prompt_components_for_request(self, api_request_row_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM prompt_components
                WHERE api_request_row_id=?
                ORDER BY estimated_tokens DESC
                """,
                (api_request_row_id,),
            )
        )

    def events(self, session_id: Optional[str] = None,
               event_type: Optional[str] = None,
               limit: int = 200) -> list[sqlite3.Row]:
        clauses, args = [], []
        if session_id:
            clauses.append("session_id=?")
            args.append(session_id)
        if event_type:
            clauses.append("event_type=?")
            args.append(event_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return list(
            self._conn.execute(
                f"SELECT * FROM events {where} ORDER BY event_at DESC LIMIT ?",
                (*args, limit),
            )
        )

    def findings(self, session_id: Optional[str] = None,
                 limit: int = 100) -> list[sqlite3.Row]:
        if session_id:
            return list(
                self._conn.execute(
                    """
                    SELECT * FROM optimizer_findings
                    WHERE session_id=?
                    ORDER BY detected_at DESC LIMIT ?
                    """,
                    (session_id, limit),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM optimizer_findings ORDER BY detected_at DESC LIMIT ?",
                (limit,),
            )
        )

    def pricing(self, profile_name: Optional[str] = None) -> list[sqlite3.Row]:
        if profile_name:
            return list(
                self._conn.execute(
                    "SELECT * FROM pricing_profiles WHERE profile_name=?",
                    (profile_name,),
                )
            )
        return list(self._conn.execute("SELECT * FROM pricing_profiles"))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _api_request_bind(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce an api_requests dict into the exact shape we bind."""
    defaults: dict[str, Any] = {k: None for k in _API_REQUEST_FIELDS}
    defaults.update({k: v for k, v in row.items() if k in _API_REQUEST_FIELDS})
    # SQLite wants ints as ints, not booleans
    for bool_key in ("streaming", "payload_truncated"):
        if defaults.get(bool_key) is not None:
            defaults[bool_key] = 1 if defaults[bool_key] else 0
    return defaults


def _tool_call_bind(row: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {k: None for k in _TOOL_CALL_FIELDS}
    defaults.update({k: v for k, v in row.items() if k in _TOOL_CALL_FIELDS})
    if defaults.get("output_truncated") is not None and isinstance(defaults["output_truncated"], bool):
        defaults["output_truncated"] = 1 if defaults["output_truncated"] else 0
    if defaults.get("file_path_stored") is not None and isinstance(defaults["file_path_stored"], bool):
        defaults["file_path_stored"] = 1 if defaults["file_path_stored"] else 0
    return defaults


def _snapshot_bind(row: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {k: None for k in _SNAPSHOT_FIELDS}
    defaults.update({k: v for k, v in row.items() if k in _SNAPSHOT_FIELDS})
    if defaults.get("hermes_native") is not None and isinstance(defaults["hermes_native"], bool):
        defaults["hermes_native"] = 1 if defaults["hermes_native"] else 0
    return defaults


def _percentile(sorted_samples: list[float], pct: float) -> float:
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    k = (len(sorted_samples) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = k - lo
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * frac


_API_REQUEST_FIELDS = (
    "session_id", "turn_id", "api_request_id", "api_call_count",
    "provider", "model", "base_url", "api_mode", "streaming",
    "started_at", "ended_at", "first_chunk_at", "duration_s", "ttft_s",
    "finish_reason", "response_model", "messages_count",
    "prompt_tokens", "prompt_tokens_provenance",
    "input_tokens", "input_tokens_provenance",
    "output_tokens", "output_tokens_provenance",
    "reasoning_tokens", "reasoning_tokens_provenance",
    "cache_read_tokens", "cache_read_tokens_provenance",
    "cache_write_tokens", "cache_write_tokens_provenance",
    "total_tokens", "total_tokens_provenance",
    "tokens_per_second", "cache_hit_ratio",
    "raw_usage_json",
    "assistant_content_chars", "assistant_tool_call_count",
    "request_hash", "response_hash",
    "prompt_visible_chars", "prompt_visible_provenance",
    "prompt_visible_tokens_est", "prompt_visible_confidence",
    "payload_truncated", "weight_cached", "weight_prompt",
    "context_tier_snapshot_id",
    "metadata_json",
)

_TOOL_CALL_FIELDS = (
    "session_id", "turn_id", "api_request_row_id",
    "tool_call_id", "tool_name", "category",
    "started_at", "ended_at", "duration_ms",
    "status", "error_type", "error_message", "exit_code",
    "output_truncated",
    "input_chars", "input_tokens_est",
    "output_chars", "output_tokens_est",
    "output_hash", "args_hash", "args_summary", "metadata_json",
    "command_family", "command_hash",
    "input_measurement_method", "output_measurement_method",
    "input_tokens", "args_keys_json",
    "path_ext", "path_hash", "path_basename", "file_path_stored",
)

_SNAPSHOT_FIELDS = (
    "taken_at", "hermes_version", "platform", "model", "base_url",
    "system_prompt_chars", "system_prompt_bytes", "system_prompt_tokens_est",
    "stable_chars", "stable_bytes", "stable_tokens_est",
    "context_chars", "context_bytes", "context_tokens_est",
    "volatile_chars", "volatile_bytes", "volatile_tokens_est",
    "skills_index_chars", "skills_index_bytes", "skills_index_tokens_est",
    "memory_chars", "memory_bytes", "memory_tokens_est",
    "user_profile_chars", "user_profile_bytes", "user_profile_tokens_est",
    "tools_count", "tools_json_bytes", "tools_json_tokens_est",
    "mcp_schemas_chars", "mcp_schemas_bytes", "mcp_schemas_tokens_est",
    "subagent_defs_chars", "subagent_defs_bytes", "subagent_defs_tokens_est",
    "other_chars", "other_bytes", "other_tokens_est",
    "tokenizer_method", "hermes_native", "metadata_json",
)


__all__ = ["Database", "DatabasePaths", "SCHEMA_VERSION"]