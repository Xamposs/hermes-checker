# Hermes Checker

A **local, non-invasive** profiler for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
and Hermes Desktop. It tells you, with as much accuracy as technically possible,
**what Hermes is spending tokens on** — without changing how Hermes runs.

Hermes Checker runs as a Hermes user plugin, captures per-API-call usage
(provider-measured token buckets, prompt-cache hits, latency, TPS), profiles
every tool call (read/write/search/terminal/test/web/...), and builds a local
SQLite store you can browse through a tiny dashboard or query from the CLI.

It does **not**:

- modify prompts, model responses, or tool outputs
- compress or rewrite anything
- reroute providers or change tool limits
- touch upstream Hermes source code
- phone home, send telemetry anywhere, or require an account

It does:

- passive observation via Hermes's official plugin/hook system
- per-session SQLite persistence under `%USERPROFILE%\.hermes-checker\`
- a small local dashboard at `http://127.0.0.1:8765/`
- a CLI for status, reporting, export, pricing projections, and rule-based
  "potential waste" hints
- explicit provenance tagging on every metric
  (`PROVIDER_MEASURED` / `HERMES_MEASURED` / `LOCALLY_CALCULATED` /
  `LOCALLY_ESTIMATED` / `UNAVAILABLE`)

## Quick start (Windows)

```powershell
cd C:\Users\xampos\Desktop\Projects\Hermes-Checker

python -m venv .venv
.venv\Scripts\activate
pip install -e .[web,dev]

hermes-checker install        # copies the user plugin into ~/.hermes/plugins
                              # and edits config.yaml (with a backup)
hermes-checker doctor         # verifies Hermes + the plugin are present

# Restart Hermes Desktop / CLI so the plugin is loaded.

hermes-checker dashboard      # http://127.0.0.1:8765/
hermes-checker status
hermes-checker sessions
hermes-checker report --session <id>
hermes-checker report --session <id> --pricing-file config\pricing.example.yaml --pricing-profile openrouter-2026-09
hermes-checker export --session <id> --format json --out session.json
```

## Architecture

```
Hermes Agent / Hermes Desktop
            |
            v
  +-- (existing plugin system) ----+
  |                                |
  v                                v
user-plugin: hermes-checker   (registered hooks:
                                on_session_start, on_session_end,
                                pre_api_request, post_api_request,
                                api_request_error, pre_tool_call,
                                post_tool_call, ...)
            |
            v
   HookCollector (in-process)
            |
            v
     SQLite (hermes-checker.db)
            |
  +---------+---------+
  |                   |
  v                   v
CLI report       FastAPI dashboard (127.0.0.1:8765)
                  /  (LIVE / SESSION / ANALYTICS / INSIGHTS)
```

The collector is intentionally in-process with Hermes — it never reaches the
network, and the dashboard reads the SQLite file directly so nothing leaves
the machine. See `docs/ARCHITECTURE.md` for the full design.

## What it measures

Per API request (provider-measured when the provider reports them, otherwise
labelled `UNAVAILABLE`):

- prompt / input / output / reasoning / cache-read / cache-write / total
  tokens
- cache hit ratio
- TTFT (when streaming; else `UNAVAILABLE`)
- tokens-per-second (when streaming)
- request duration
- finish reason
- model and provider

Per session (locally attributed):

- provider totals
- prompt-component attribution (SYSTEM, TOOLS_SCHEMA, SKILLS, MEMORY,
  PROJECT_INSTRUCTIONS, USER_MESSAGES, ASSISTANT_HISTORY, TOOL_RESULTS, OTHER)
- per-category tool breakdown (file_read, file_write, search, terminal,
  test, build, git, web, mcp, memory, skill, other)
- rule-based findings (POTENTIAL_WASTE / HIGH_OVERHEAD / REPEATED_CONTENT /
  OBSERVATION)

See `docs/METRICS.md` for the full metric dictionary and provenance rules.

## Privacy & security

V1 only persists **metadata**: counts, hashes, character lengths, timing,
classification, error category, exit status. It does **not** persist full
prompts, full responses, source file contents, or terminal transcripts.

Defense in depth:

- A second-pass secret sanitizer runs on every field before write
  (`api_key`, `authorization`, `bearer ...`, `sk-...`, `pk-...`, JWTs,
  AWS keys, GitHub PATs, Slack tokens).
- The dashboard binds to `127.0.0.1` only. It is not exposed on the LAN.
- The `.gitignore` excludes the runtime database, `.env*`, and any
  pricing/profile exports that could contain secrets.

## Experiments and A/B comparisons

Hermes Checker attaches an `experiment` label to every recorded session.
Set it via the environment variable `HERMES_CHECKER_EXPERIMENT` or pass
`--experiment` to the install command:

```powershell
set HERMES_CHECKER_EXPERIMENT=baseline-minimax-direct
hermes-checker dashboard
```

The CLI/dashboard lets you filter by experiment, so you can later compare:

- `baseline-minimax-direct` — current free baseline through OpenCode
- `deepseek-openrouter` — same workload, DeepSeek V4 Flash through OpenRouter
- `omniroute-rtk` — same workload, routed through OmniRoute RTK (future)
- `optimized-tools-v1` — same workload, with optimized tool limits (future)

The cost engine reads a user-editable YAML profile (see
`config/pricing.example.yaml`) so you can ask "what would this session have
cost on DeepSeek V4 Flash?" without changing providers.

## Limitations

V1 is intentionally lightweight. Known limitations are listed in
`docs/LIMITATIONS.md`. The big ones:

- Component attribution is approximate — Hermes doesn't expose per-section
  token counts. We tokenize the messages Hermes shipped and label the
  output as `LOCALLY_ESTIMATED`.
- TPS and TTFT only make sense for streaming responses; non-streaming
  responses report them as `UNAVAILABLE`.
- We pin Hermes's hook contract for **agent-side** measurements. Anything
  Hermes doesn't surface stays out of the database — we never fabricate.

## Development

```powershell
pytest -q
ruff check .
```

Tests cover schema migrations, usage normalization, cache calculations,
pricing, tokenizer fallback, tool classification, attribution, sanitization,
report generation, the CLI parser, and the install/uninstall helpers.

## License

MIT — see `LICENSE`.