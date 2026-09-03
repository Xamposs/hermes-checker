"""SQLite schema for Hermes Checker.

The schema is intentionally append-only: every new column lands as a new
migration and ``schema_version`` is bumped.  Old rows are never rewritten.

Migrations
----------

- **v1** — initial schema (sessions, turns, api_requests,
  prompt_components, tool_calls, models, providers, pricing_profiles,
  events, optimizer_findings).
- **v2** — adds the new tables/columns needed for V1.1:

  * ``tool_calls`` gets the per-call command-family / command-hash /
    input-tokens / input-chars / input-method / output-method fields
    needed for command-aware tool classification and proper
    input/output accounting.
  * ``api_requests`` gets ``prompt_visible_chars`` and
    ``payload_truncated`` so we can detect Hermes-side
    ``HERMES_PLUGIN_PAYLOAD_MAX_CHARS`` truncation and refuse to
    silently treat the visible portion as the full prompt.
  * ``api_requests`` gets ``weight_cached`` and
    ``weight_prompt`` so we can compute the token-weighted session
    cache hit ratio without re-summing.
  * ``sessions`` gets ``context_breakdown_json`` (Hermes-native
    stable/context/volatile tier + skills index + tools json
    snapshots) so the dashboard can show fixed-overhead without
    recomputing offline.
  * ``api_requests`` gets ``context_tier_snapshot_id`` so we can
    join each request to the breakdown snapshot that was live at
    the time.
  * New table ``static_prompt_snapshots`` — one row per
    ``hermes-checker snapshot`` run. Captures the Hermes-native
    per-tier token breakdown (system, tools, skills index, memory,
    user profile, MCP, subagent defs) plus per-skill and per-toolset
    sub-tables.
  * New table ``skill_events`` — one row per ``on_skill_lifecycle``
    fact (skill_name, action, session_id, task_id, turn_id,
    detected_at, use_count, reused, reuse_after_patch).
  * New table ``context_deltas`` — one row per
    LOCALLY_ATTRIBUTED_CONTEXT_DELTA between consecutive
    ``api_requests`` (previous_id, current_id, provider_delta,
    explained_tokens, coverage, contributors_json).
  * New table ``app_config`` — small key/value store for persistent
    Hermes Checker settings (current ``experiment`` label, etc.).
  * New table ``self_overhead_samples`` — per-callback-duration
    samples so we can self-monitor and surface >50ms warnings.

- **v3** — add the ``provenance`` column to ``prompt_components``
  (Issue 7) so component rows can carry the ``HERMES_NATIVE_ESTIMATE``
  vs ``LOCALLY_ESTIMATED`` tag without requiring every caller to
  recompute it from the row's own contents.

Provenance
----------

Every numeric column carries an explicit ``_provenance`` text column
(or the row carries ``provenance``) so we can label whether the value
came from the provider, Hermes, or was locally estimated.

Five labels are in use:
``PROVIDER_MEASURED`` / ``HERMES_MEASURED`` / ``HERMES_NATIVE_ESTIMATE`` /
``LOCALLY_CALCULATED`` / ``LOCALLY_ESTIMATED`` / ``UNAVAILABLE``.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

SCHEMA_VERSION = 3

# Each migration is a SQL string.  ``apply_migrations`` runs every
# migration whose id is strictly greater than the row in ``schema_meta``.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL UNIQUE,
        profile         TEXT,
        platform        TEXT,
        started_at      REAL NOT NULL,
        ended_at        REAL,
        experiment      TEXT,
        metadata_json   TEXT
    );
    CREATE INDEX ix_sessions_started_at ON sessions(started_at);

    CREATE TABLE turns (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL,
        turn_id         TEXT,
        started_at      REAL NOT NULL,
        ended_at        REAL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
    CREATE INDEX ix_turns_session ON turns(session_id);
    CREATE INDEX ix_turns_started_at ON turns(started_at);

    CREATE TABLE models (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        provider    TEXT NOT NULL,
        model       TEXT NOT NULL,
        first_seen  REAL NOT NULL,
        metadata_json TEXT,
        UNIQUE(provider, model)
    );

    CREATE TABLE providers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        provider    TEXT NOT NULL UNIQUE,
        base_url    TEXT,
        first_seen  REAL NOT NULL
    );

    CREATE TABLE api_requests (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id               TEXT NOT NULL,
        turn_id                  TEXT,
        api_request_id           TEXT,
        api_call_count           INTEGER,
        provider                 TEXT,
        model                    TEXT,
        base_url                 TEXT,
        api_mode                 TEXT,
        streaming                INTEGER,            -- 0/1
        started_at               REAL NOT NULL,
        ended_at                 REAL,
        first_chunk_at           REAL,
        duration_s               REAL,
        ttft_s                   REAL,
        finish_reason            TEXT,
        response_model           TEXT,
        messages_count           INTEGER,

        -- Provider-measured tokens (source: CanonicalUsage -> dict).
        -- The "_provenance" columns record WHERE each bucket came from.
        prompt_tokens            INTEGER,
        prompt_tokens_provenance TEXT,
        input_tokens             INTEGER,
        input_tokens_provenance  TEXT,
        output_tokens            INTEGER,
        output_tokens_provenance TEXT,
        reasoning_tokens         INTEGER,
        reasoning_tokens_provenance TEXT,
        cache_read_tokens        INTEGER,
        cache_read_tokens_provenance TEXT,
        cache_write_tokens       INTEGER,
        cache_write_tokens_provenance TEXT,
        total_tokens             INTEGER,
        total_tokens_provenance  TEXT,

        -- Derived metrics (calculated locally)
        tokens_per_second        REAL,
        cache_hit_ratio          REAL,

        -- Caches of raw payload fields we don't always surface
        raw_usage_json           TEXT,
        assistant_content_chars  INTEGER,
        assistant_tool_call_count INTEGER,
        request_hash             TEXT,
        response_hash            TEXT,

        -- Anything we want to keep but don't want a column for
        metadata_json            TEXT,

        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
    CREATE INDEX ix_api_requests_session ON api_requests(session_id);
    CREATE INDEX ix_api_requests_started_at ON api_requests(started_at);
    CREATE INDEX ix_api_requests_provider_model ON api_requests(provider, model);
    CREATE INDEX ix_api_requests_request_hash ON api_requests(request_hash);

    CREATE TABLE prompt_components (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        api_request_row_id  INTEGER NOT NULL,
        component           TEXT NOT NULL,
        characters          INTEGER NOT NULL,
        bytes               INTEGER NOT NULL,
        estimated_tokens    INTEGER NOT NULL,
        measurement_method  TEXT NOT NULL,            -- TOKENIZED / ESTIMATED
        confidence          REAL NOT NULL,            -- 0..1
        source_identifier   TEXT,                     -- e.g. role / tool_call_id / prefix hash
        FOREIGN KEY (api_request_row_id) REFERENCES api_requests(id)
    );
    CREATE INDEX ix_components_request ON prompt_components(api_request_row_id);

    CREATE TABLE tool_calls (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          TEXT NOT NULL,
        turn_id             TEXT,
        api_request_row_id  INTEGER,                 -- best-effort link to the API request
        tool_call_id        TEXT,
        tool_name           TEXT NOT NULL,
        category            TEXT NOT NULL,            -- file_read / search / terminal / ...
        started_at          REAL NOT NULL,
        ended_at            REAL,
        duration_ms         REAL,
        status              TEXT,                     -- ok / error / needs-auth / timeout
        error_type          TEXT,
        error_message       TEXT,
        exit_code           INTEGER,
        output_truncated    INTEGER,                  -- 0/1/UNKNOWN
        input_chars         INTEGER,
        input_tokens_est    INTEGER,
        output_chars        INTEGER,
        output_tokens_est   INTEGER,
        output_hash         TEXT,
        args_hash           TEXT,
        args_summary        TEXT,                     -- short string label, no full args
        metadata_json       TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id),
        FOREIGN KEY (api_request_row_id) REFERENCES api_requests(id)
    );
    CREATE INDEX ix_tool_calls_session ON tool_calls(session_id);
    CREATE INDEX ix_tool_calls_started_at ON tool_calls(started_at);
    CREATE INDEX ix_tool_calls_category ON tool_calls(category);
    CREATE INDEX ix_tool_calls_output_hash ON tool_calls(output_hash);

    CREATE TABLE pricing_profiles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_name    TEXT NOT NULL UNIQUE,
        model           TEXT NOT NULL,
        input_per_million_usd     REAL,
        cached_input_per_million_usd REAL,
        cache_write_per_million_usd REAL,
        output_per_million_usd    REAL,
        reasoning_per_million_usd REAL,
        request_cost_usd          REAL,
        notes         TEXT
    );

    CREATE TABLE events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT,
        api_request_row_id INTEGER,
        tool_call_row_id   INTEGER,
        event_type      TEXT NOT NULL,
        event_at        REAL NOT NULL,
        payload_json    TEXT
    );
    CREATE INDEX ix_events_session ON events(session_id);
    CREATE INDEX ix_events_type ON events(event_type);
    CREATE INDEX ix_events_at ON events(event_at);

    CREATE TABLE optimizer_findings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT,
        finding_kind    TEXT NOT NULL,                -- repeated_full_file / large_terminal / etc.
        severity        TEXT NOT NULL,                -- POTENTIAL_WASTE / HIGH_OVERHEAD / ...
        confidence      REAL NOT NULL,
        detected_at     REAL NOT NULL,
        evidence_json   TEXT,
        message         TEXT
    );
    CREATE INDEX ix_findings_session ON optimizer_findings(session_id);
    CREATE INDEX ix_findings_kind ON optimizer_findings(finding_kind);
    """),
    (2, """
    -- V1.1 additions (Issues 6, 7, 8, 9, 10, 12, 13, 14, 16, 19).
    --
    -- The v1.1 hardening pass needs:
    --   * per-tool-call command family / command hash / input & output
    --     tokenisation method (so a `pytest`, `npm test`, `git diff`,
    --     `npm run build` call are distinguishable from a generic
    --     `terminal` call).
    --   * prompt-truncation awareness so we never attribute the
    --     truncated visible portion of a pre_api_request payload as
    --     "the full prompt".
    --   * cached/prompt totals on api_requests so the session-level
    --     token-weighted cache hit ratio is one sum() not N.
    --   * a static prompt snapshot per Hermes configuration
    --     (stable / context / volatile tiers, skills index, memory,
    --     user profile, tools, MCP, subagent defs) — captured by
    --     `hermes-checker snapshot` from Hermes's own
    --     compute_prompt_breakdown().
    --   * per-skill lifecycle facts.
    --   * per-pair context delta between consecutive api_requests so
    --     we can show "terminal +X%, file read +Y%".
    --   * a tiny key/value config store for the persistent
    --     experiment label.
    --   * profiler self-overhead samples so we can detect a
    --     slow-callback regression in our own code.

    ALTER TABLE api_requests
        ADD COLUMN prompt_visible_chars INTEGER;          -- characters actually seen in the pre hook
    ALTER TABLE api_requests
        ADD COLUMN prompt_visible_provenance TEXT;       -- PROVIDER_MEASURED if the full messages
                                                          -- arrived; LOCALLY_ESTIMATED if Hermes
                                                          -- truncated via HERMES_PLUGIN_PAYLOAD_MAX_CHARS
    ALTER TABLE api_requests
        ADD COLUMN prompt_visible_tokens_est INTEGER;    -- local tokenisation of visible chars
    ALTER TABLE api_requests
        ADD COLUMN prompt_visible_confidence REAL;       -- 0..1; drops sharply when truncated
    ALTER TABLE api_requests
        ADD COLUMN payload_truncated INTEGER;            -- 1 if Hermes replaced the payload
                                                          -- with the _truncated preview sentinel
    ALTER TABLE api_requests
        ADD COLUMN weight_cached INTEGER;                -- redundant with cache_read_tokens,
                                                          -- but lets the session query stay
                                                          -- in one table without joins
    ALTER TABLE api_requests
        ADD COLUMN weight_prompt INTEGER;
    ALTER TABLE api_requests
        ADD COLUMN context_tier_snapshot_id INTEGER;     -- FK -> static_prompt_snapshots.id

    ALTER TABLE tool_calls
        ADD COLUMN command_family TEXT;                  -- git / pytest / npm-test / ruff / rg / ...
    ALTER TABLE tool_calls
        ADD COLUMN command_hash TEXT;                    -- SHA256 of the canonicalised command line
    ALTER TABLE tool_calls
        ADD COLUMN input_measurement_method TEXT;         -- TIKTOKEN / HEURISTIC / UNKNOWN
    ALTER TABLE tool_calls
        ADD COLUMN output_measurement_method TEXT;
    ALTER TABLE tool_calls
        ADD COLUMN input_tokens INTEGER;                 -- the prompt-side contribution
    ALTER TABLE tool_calls
        ADD COLUMN args_keys_json TEXT;                   -- JSON list of top-level argument keys
    ALTER TABLE tool_calls
        ADD COLUMN path_ext TEXT;
    ALTER TABLE tool_calls
        ADD COLUMN path_hash TEXT;                       -- SHA256 of absolute path (no content)
    ALTER TABLE tool_calls
        ADD COLUMN path_basename TEXT;                   -- basename only (no directory tree)
    ALTER TABLE tool_calls
        ADD COLUMN file_path_stored INTEGER;              -- 1 if the path was kept; 0 redacted

    CREATE TABLE static_prompt_snapshots (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        taken_at                    REAL NOT NULL,
        hermes_version              TEXT,
        platform                    TEXT,
        model                       TEXT,
        base_url                    TEXT,
        system_prompt_chars         INTEGER,
        system_prompt_bytes         INTEGER,
        system_prompt_tokens_est    INTEGER,
        stable_chars                INTEGER,
        stable_bytes                INTEGER,
        stable_tokens_est           INTEGER,
        context_chars               INTEGER,
        context_bytes               INTEGER,
        context_tokens_est          INTEGER,
        volatile_chars               INTEGER,
        volatile_bytes               INTEGER,
        volatile_tokens_est          INTEGER,
        skills_index_chars          INTEGER,
        skills_index_bytes          INTEGER,
        skills_index_tokens_est     INTEGER,
        memory_chars                INTEGER,
        memory_bytes                INTEGER,
        memory_tokens_est           INTEGER,
        user_profile_chars          INTEGER,
        user_profile_bytes          INTEGER,
        user_profile_tokens_est     INTEGER,
        tools_count                 INTEGER,
        tools_json_bytes             INTEGER,
        tools_json_tokens_est        INTEGER,
        mcp_schemas_chars            INTEGER,
        mcp_schemas_bytes            INTEGER,
        mcp_schemas_tokens_est       INTEGER,
        subagent_defs_chars         INTEGER,
        subagent_defs_bytes         INTEGER,
        subagent_defs_tokens_est    INTEGER,
        other_chars                 INTEGER,
        other_bytes                 INTEGER,
        other_tokens_est            INTEGER,
        tokenizer_method            TEXT,
        hermes_native               INTEGER,             -- 1 if computed via Hermes
                                                          -- compute_prompt_breakdown, 0 if fallback
        metadata_json               TEXT
    );
    CREATE INDEX ix_static_snapshots_taken_at ON static_prompt_snapshots(taken_at);
    CREATE INDEX ix_static_snapshots_model ON static_prompt_snapshots(model);

    CREATE TABLE static_skill_breakdowns (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id         INTEGER NOT NULL,
        skill_name          TEXT NOT NULL,
        index_line_chars    INTEGER,
        index_line_bytes    INTEGER,
        index_line_tokens_est INTEGER,
        skill_md_chars      INTEGER,
        skill_md_bytes      INTEGER,
        skill_md_tokens_est INTEGER,
        rank_in_index       INTEGER,
        FOREIGN KEY (snapshot_id) REFERENCES static_prompt_snapshots(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_static_skill_breakdowns_snapshot ON static_skill_breakdowns(snapshot_id);

    CREATE TABLE static_toolset_breakdowns (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id         INTEGER NOT NULL,
        toolset_name        TEXT NOT NULL,
        tool_count          INTEGER,
        schema_chars        INTEGER,
        schema_bytes        INTEGER,
        schema_tokens_est   INTEGER,
        FOREIGN KEY (snapshot_id) REFERENCES static_prompt_snapshots(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_static_toolset_breakdowns_snapshot ON static_toolset_breakdowns(snapshot_id);

    CREATE TABLE skill_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT,
        task_id         TEXT,
        turn_id         TEXT,
        skill_name      TEXT NOT NULL,
        action          TEXT NOT NULL,                   -- loaded / created / patched / archived / ...
        detected_at     REAL NOT NULL,
        use_count       INTEGER,
        reused          INTEGER,
        reuse_after_patch INTEGER,
        metadata_json   TEXT
    );
    CREATE INDEX ix_skill_events_session ON skill_events(session_id);
    CREATE INDEX ix_skill_events_skill ON skill_events(skill_name);
    CREATE INDEX ix_skill_events_detected_at ON skill_events(detected_at);

    CREATE TABLE context_deltas (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id                  TEXT NOT NULL,
        previous_api_request_id     INTEGER,
        current_api_request_id      INTEGER NOT NULL,
        provider_delta_tokens       INTEGER,             -- current.prompt - previous.prompt
        explained_tokens            INTEGER,             -- local attribution explained sum
        unexplained_tokens          INTEGER,             -- provider_delta - explained (can be negative)
        coverage                    REAL,                -- explained_tokens / provider_delta
        contributors_json           TEXT,                 -- [{"component": "TOOL_RESULTS", "tokens": 9200, "pct": 56}, ...]
        detected_at                 REAL NOT NULL,
        confidence                  REAL,
        metadata_json               TEXT,
        FOREIGN KEY (previous_api_request_id) REFERENCES api_requests(id),
        FOREIGN KEY (current_api_request_id) REFERENCES api_requests(id)
    );
    CREATE INDEX ix_context_deltas_session ON context_deltas(session_id);
    CREATE INDEX ix_context_deltas_current ON context_deltas(current_api_request_id);

    CREATE TABLE app_config (
        key             TEXT PRIMARY KEY,
        value           TEXT NOT NULL,
        updated_at      REAL NOT NULL
    );

    CREATE TABLE self_overhead_samples (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        callback_name   TEXT NOT NULL,                   -- e.g. post_api_request
        duration_ms     REAL NOT NULL,
        sampled_at      REAL NOT NULL
    );
    CREATE INDEX ix_self_overhead_callback ON self_overhead_samples(callback_name);
    CREATE INDEX ix_self_overhead_at ON self_overhead_samples(sampled_at);
    """),
    (3, """
    -- v3: add the ``provenance`` column to prompt_components so the
    -- reporting layer can label each component row with one of
    -- PROVIDER_MEASURED / HERMES_MEASURED / HERMES_NATIVE_ESTIMATE /
    -- LOCALLY_CALCULATED / LOCALLY_ESTIMATED / UNAVAILABLE without
    -- having to recompute it from the row's own contents.
    ALTER TABLE prompt_components ADD COLUMN provenance TEXT;
    -- Backfill: V1 / V2 collectors wrote rows that were always
    -- locally-estimated. Default them so the dashboard has a value.
    UPDATE prompt_components
       SET provenance = 'LOCALLY_ESTIMATED'
     WHERE provenance IS NULL;
    """),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations. Returns the new schema version."""
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    cur.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
    row = cur.fetchone()
    current = int(row[0]) if row else 0

    pending = [(v, sql) for v, sql in _MIGRATIONS if v > current]
    for v, sql in pending:
        conn.executescript(sql)
    if pending:
        new_version = max(v for v, _ in pending)
        cur.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(new_version),),
        )
        conn.commit()
        return new_version
    return current


def initialise_database(conn: sqlite3.Connection) -> int:
    """Create tables if missing and return the current schema version."""
    return apply_migrations(conn)


def iter_migration_ids() -> Iterable[int]:
    return [v for v, _ in _MIGRATIONS]