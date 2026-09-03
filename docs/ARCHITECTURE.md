# Hermes Checker — Architecture

This document explains how Hermes Checker is wired together. The goal is
to be observable: every component should be greppable, every data flow
should follow the rule "no upstream modification of Hermes".

## Big picture

```
┌────────────────────────┐  plugin/hooks   ┌────────────────────┐
│  Hermes Agent /        │ ──────────────► │  hermes_checker    │
│  Hermes Desktop        │   (per-API-call │  user plugin       │
│  (Python backend)      │    per-tool     │  (read-only)       │
│                        │    per-session) │                    │
└────────────────────────┘                 └─────────┬──────────┘
                                                      │ SQL inserts
                                                      ▼
                                            ┌────────────────────┐
                                            │  SQLite DB         │
                                            │  ~/.hermes-checker/│
                                            └─────────┬──────────┘
                                                      │ read-only
                                              ┌───────┴────────┐
                                              ▼                ▼
                                        ┌──────────┐    ┌────────────┐
                                        │  CLI     │    │  Dashboard │
                                        │  report  │    │  FastAPI   │
                                        │  status  │    │  127.0.0.1 │
                                        │  export  │    │  :8765     │
                                        └──────────┘    └────────────┘
```

## Component responsibilities

### `hermes_checker/integrations/hermes_plugin/`

The user plugin that Hermes Agent loads. It is intentionally tiny:

- `register(ctx)` is the only public symbol; Hermes calls it once at
  startup.
- For each Hermes hook we care about, we register a thin wrapper that
  delegates to the in-process :class:`HookCollector`.
- The plugin adds its own directory to `sys.path` and imports the
  full :mod:`hermes_checker` package from a sibling copy that the
  install command placed there.

### `hermes_checker/collector/`

`HookCollector` is the heart of the system:

- `on_session_start / on_session_end / on_session_finalize` upsert a
  `sessions` row.
- `pre_api_request` records the in-flight API request and caches the
  outgoing messages for later attribution. `post_api_request` flushes
  the row, attaches the `usage` summary with provenance, and runs the
  local component attribution.
- `pre_tool_call` / `post_tool_call` correlate on `tool_call_id` and
  record the call with category, output hash, and timing.
- `api_request_error` records the failure and discards the pending
  request (so we don't leak in-flight state on errors).

The collector never blocks the Hermes event loop for more than a
SQLite write. WAL mode keeps the dashboard's reads non-blocking.

### `hermes_checker/accounting/`

Pure-Python helpers, fully testable in isolation:

- `tokenizer` — wraps `tiktoken` when installed, falls back to a
  `chars/4` heuristic. The choice is recorded on every attribution row.
- `usage` — translates whatever shape Hermes hands us into a
  `UsageSummary` whose every field carries its own provenance tag.
- `sanitize` — second-pass secret scrubbing before any value touches
  the database.
- `attribution` — heuristic role/content classification that buckets
  each message into a component (SYSTEM, TOOLS_SCHEMA, SKILLS,
  MEMORY, PROJECT_INSTRUCTIONS, USER_MESSAGES, ASSISTANT_HISTORY,
  TOOL_RESULTS, OTHER).
- `pricing` — loads user YAML profiles, computes projected cost.
  Uses PyYAML when available, otherwise an inline subset parser.

### `hermes_checker/analysis/`

Rule-based analyzer with no LLM calls:

- `Analyzer.analyze_request` runs on every `post_api_request` when
  the user opts in (config flag). It looks for large context jumps
  and cache-miss bursts.
- `Analyzer.analyze_session` runs on demand from the CLI / dashboard
  and surfaces category dominance, repeated tool output, and large
  terminal outputs. Every finding is written to `optimizer_findings`
  with a confidence score so the UI/CLI can show evidence.

### `hermes_checker/storage/`

SQLite persistence with append-only migrations:

- `Database` opens a connection in WAL mode and applies pending
  migrations on open.
- Typed writer methods (`insert_api_request`, `insert_tool_call`,
  `insert_prompt_components`, etc.) keep the schema migration story
  simple — columns get added in new migrations, never rewritten.
- Read-only connections are used by the dashboard so the collector
  can keep writing while the user is browsing.

### `hermes_checker/web/`

FastAPI app. Four views in the SPA shell, JSON endpoints for each.
Binds to `127.0.0.1` by default — never `0.0.0.0`.

### `hermes_checker/cli.py`

The `hermes-checker` command. Subcommands:

| Command        | Purpose                                          |
|----------------|--------------------------------------------------|
| `doctor`       | Verify installation + integration                |
| `status`       | One-line summary of the database                 |
| `dashboard`    | Start the local FastAPI dashboard                |
| `report`       | Print a session report (with optional pricing)   |
| `sessions`     | List observed sessions                           |
| `export`       | Export a session as JSON or CSV                  |
| `pricing`      | Inspect a pricing YAML file                      |
| `install`      | Copy the user plugin into Hermes and edit config |
| `uninstall`    | Undo a previous install                          |
| `analyze`      | Run the analyzer on a session                    |

### `hermes_checker/install.py`

Idempotent install / uninstall / diagnose helpers:

- `_hermes_home` finds `%LOCALAPPDATA%\hermes` (or `HERMES_HOME` /
  `~/.hermes`).
- `_inject_enabled` edits `config.yaml` surgically: it finds the
  `plugins:` block, looks for an existing `enabled:` child, and
  appends `hermes-checker` to it (or converts `enabled: []` to a
  multi-line list). A backup of the original file is always created
  first.
- `diagnose` powers `hermes-checker doctor`.

## Data flow for a single API call

1. Hermes prepares the chat-completion request and is about to send
   it. Its plugin system calls every registered `pre_api_request`
   hook with the request body.
2. Hermes Checker's `pre_api_request` builds a `_PendingApiRequest`
   keyed by `(session_id, api_request_id)` and caches the messages
   payload for later attribution.
3. Hermes dispatches the HTTP call to the provider. We don't observe
   this — we just wait for the post hook.
4. Hermes processes the response, normalises it into a
   `CanonicalUsage`, and calls every registered `post_api_request`
   hook with a sanitized payload that includes the usage dict and
   the original messages.
5. Our `post_api_request`:
   - extracts the usage summary with provenance tags
   - computes TTFT, generation time, tokens-per-second, cache hit
   - flushes a row to `api_requests`
   - runs `attribute_messages` against the cached messages and
     writes rows to `prompt_components`
   - if the analyzer is enabled, runs `analyze_request`
6. The dashboard, the CLI, and the report builder can read the rows
   at any time without affecting Hermes.

## Why a plugin, not a log scraper

The Hermes plugin system is the only stable integration surface:

- Hook payloads are well-documented and sanitized for us already.
- We get the sanitized `messages` payload and the `CanonicalUsage`
  dict directly, so we never need to parse provider-specific JSON
  bodies.
- We get a `turn_id`, `api_request_id`, `task_id`, and `session_id`
  for free, so attribution is straightforward.
- We don't depend on Hermes log format, which can change.

If the plugin can't be enabled (e.g. a custom Hermes build that
disables the plugin system), Hermes Checker reports the situation
in `doctor` and prints the manual recovery steps. We do not
implement a secondary passive observer because that would either
re-implement the hook system or parse logs.

## What we never do

- modify prompts or responses
- compress or rewrite anything
- reroute providers
- change tool output
- change tool limits
- touch upstream Hermes source
- make outbound network calls
- require an account
- run anything in the hot path that could measurably slow Hermes

## Performance

- The collector's `pre_api_request` and `post_api_request` callbacks
  are O(1) in terms of work they do inline — they only enqueue /
  write a single SQLite row. WAL mode keeps write latency in the
  low microseconds for a single row.
- The tokenizer only loads once per process (singleton per model).
- The analyzer is opt-in. When enabled, it runs after the post hook
  commits, so the hook callback never blocks on the analyzer.
- The dashboard uses a read-only connection and never writes, so
  there's no lock contention.
## V1.1 changes (this revision)

- The collector gains a hermes_checker.hermes_native bridge that
  invokes Hermes's own offline prompt-size machinery either in-process
  (when running inside Hermes's venv) or via a short-lived subprocess
  (otherwise). The bridge is what backs hermes-checker snapshot.
- ccounting.attribution.attribute_messages now does
  **section-aware** splitting of the role=system message (tools,
  skills, memory, user_profile, project instructions, MCP, subagents)
  by header detection and conservative content-shape checks; the
  whole message is upgraded from SYSTEM to the matching component
  when one of the cheap recognisers fires.
- The 	ool_calls table now carries command_family,
  command_hash, input_measurement_method, output_measurement_method,
  input_tokens, rgs_keys_json, path_ext, path_hash,
  path_basename, ile_path_stored. classify_tool(name, args) is
  command-aware: a pytest invocation lands in 	est, a
  git diff in git, an 
pm run build in uild, etc.
- post_api_request populates prompt_visible_chars,
  prompt_visible_provenance, prompt_visible_tokens_est,
  prompt_visible_confidence, payload_truncated,
  weight_cached, weight_prompt. The collector detects the
  _sanitize_hook_payload truncation sentinel and suppresses
  attribution for that request.
- A new context_deltas table holds the LOCALLY_ATTRIBUTED delta
  between consecutive API requests: provider delta, explained sum,
  unexplained gap, coverage fraction, and a per-component contributors
  list. The collector fills this in whenever post_api_request is
  called with a non-truncated payload.
- A new pp_config key-value table backs the persistent experiment
  label. The hermes-checker experiment set|show|clear subcommands
  read/write it. The collector reads experiment on every session
  start.
- A new skill_events table logs every on_skill_lifecycle payload
  (action, skill_name, use_count, reused, reuse_after_patch).
- A new self_overhead_samples table lets the collector time its own
  callbacks. The HookCollector._record_self_overhead and
  HookCollector._timed helpers surface a >50ms warning when an
  observer callback becomes a perf problem.
- The report layer (eporting.py) now exposes the
  SessionReport.by_provider_model and
  SessionReport.by_context_bucket lists plus P50/P95 percentiles
  for latency, TTFT, and TPS. The token-weighted session cache hit
  ratio (cache_hit_ratio_weighted) is the primary metric; the
  unweighted mean is kept for comparison.
- A new hermes-checker snapshot CLI subcommand persists the static
  prompt breakdown to static_prompt_snapshots plus
  static_skill_breakdowns and static_toolset_breakdowns rows.
- A new hermes-checker experiment CLI subcommand set/show/clear
  backs the persistent experiment label.
- hermes-checker doctor was hardened. Each check is on its own line
  with [PASS] / [FAIL] / [WARN] / [SKIP], and the "real PluginManager
  discovers plugin" check actually spawns Hermes's Python and runs
  _discover_and_load_inner on a real sandbox home, with no substring
  searching of config.yaml for the substring "hermes-checker".
