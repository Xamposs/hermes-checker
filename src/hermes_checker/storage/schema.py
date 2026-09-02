"""SQLite schema for Hermes Checker.

The schema is intentionally append-only: every new column lands as a new
migration and ``schema_version`` is bumped.  Old rows are never rewritten.

Tables
------

- ``sessions``             — one row per observed session (Hermes ``on_session_*``)
- ``turns``                — one row per conversation turn (best-effort; Hermes
                             does not directly expose a turn-id column on
                             ``post_api_request``)
- ``api_requests``         — one row per Hermes ``post_api_request`` (a single
                             HTTP call to the provider). The provider-measured
                             token buckets live here.
- ``prompt_components``     — one row per attributed component of the prompt
                             (system, tools, memory, …) — what Hermes Checker
                             could attribute LOCALLY.
- ``tool_calls``           — one row per Hermes ``post_tool_call``.
- ``tool_outputs``         — never the raw output — only hash + size + category.
- ``models`` / ``providers`` / ``pricing_profiles`` — catalogs.
- ``events``               — generic key/value stream for things that don't fit
                             a typed table.
- ``optimizer_findings``   — rule-based findings emitted by the analyzer.

Provenance
----------

Every numeric column carries an explicit ``_provenance`` text column
(or the row carries ``provenance``) so we can label whether the value
came from the provider, Hermes, or was locally estimated.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

SCHEMA_VERSION = 1

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