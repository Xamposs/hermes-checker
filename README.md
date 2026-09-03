# Hermes Checker (V1.1)

A **local, non-invasive** profiler for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
and Hermes Desktop. It tells you, with as much accuracy as technically possible,
**what Hermes is spending tokens on** — without changing how Hermes runs.

Hermes Checker runs as a Hermes user plugin, captures per-API-call usage
(provider-measured token buckets, prompt-cache hits, latency, TPS), profiles
every tool call (read/write/search/terminal/test/build/git/lint/web/mcp/...),
and breaks the prompt down into components (system / tools / skills / memory /
user profile / MCP / subagent defs) using Hermes's own offline breakdown code
when available.

It does **not**:

- modify prompts, model responses, or tool outputs
- compress or rewrite anything
- reroute providers or change tool limits
- touch upstream Hermes source code
- phone home, send telemetry anywhere, or require an account
- call LLMs to do analysis

It does:

- passive observation via Hermes's official plugin/hook system
- per-session SQLite persistence under `%USERPROFILE%\.hermes-checker\`
- a small local dashboard at `http://127.0.0.1:8765/`
- a CLI for status, reports, exports, pricing projections, and a
  static-prompt snapshot
- explicit provenance tagging on every metric — six labels, not five
- token-weighted session cache hit ratio (the metric that matters
  when you compare providers)
- P50 / P95 percentiles for latency, TTFT, and TPS
- per-(provider, model) and per-context-size-bucket performance breakdowns
- command-aware tool classification (so `pytest`, `git diff`,
  `npm run build`, `ruff check` and `rg` end up in the right buckets
  instead of one "terminal" pile)
- context-delta attribution between consecutive API requests
  ("what grew the prompt by 27k tokens?")
- static prompt snapshot via Hermes's own `compute_prompt_breakdown`
  — no LLM, no network
- skill-lifecycle facts (loaded / created / patched / archived …)
- detection of Hermes-side payload truncation so we never treat
  a partial visible payload as "the full prompt"

## Six provenance labels

Hermes Checker distinguishes:

| Label | Meaning |
|-------|---------|
| `PROVIDER_MEASURED` | Read directly from the provider's response (Hermes's `CanonicalUsage`). Authoritative. |
| `HERMES_MEASURED` | Read from a Hermes runtime field (count, timing). |
| `HERMES_NATIVE_ESTIMATE` | Computed by Hermes's own offline breakdown code (e.g. `compute_prompt_breakdown`). |
| `LOCALLY_CALCULATED` | Derived locally from measured inputs (e.g. `prompt = input + cache_read + cache_write`). |
| `LOCALLY_ESTIMATED` | Tokenised/estimated locally (e.g. message-level breakdown using tiktoken or a chars/4 fallback). |
| `UNAVAILABLE` | The source didn't provide this; left NULL. |

**Provider tokens are the billing ground truth.** Component
attribution is **always** labelled — it is an honest decomposition of
the visible payload, never a provider-billing number.

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
hermes-checker snapshot       # Hermes-native static prompt breakdown
hermes-checker report --session <id>
hermes-checker report --session <id> `
  --pricing-file config\pricing.example.yaml `
  --pricing-profile openrouter-2026-09
hermes-checker export --session <id> --format json --out session.json
hermes-checker pricing config\pricing.example.yaml --profile openrouter-2026-09
hermes-checker experiment set baseline-minimax-direct
hermes-checker experiment show
```

The plugin is installed automatically at
`%LOCALAPPDATA%\hermes\plugins\hermes-checker\` with a properly-formatted
`plugin.yaml` manifest, so the real Hermes `PluginManager` discovers
and loads it. The `hermes-checker install` command backs up the user's
`config.yaml` and surgically adds the `hermes-checker` entry to
`plugins.enabled`.

`hermes-checker doctor` runs the **real** Hermes `PluginManager`
discovery subprocess — no string search, no fake PASS.

## What the snapshot command gives you

`hermes-checker snapshot` runs Hermes's own
`compute_prompt_breakdown` (offline, no LLM, no network) and stores
the result in `static_prompt_snapshots` plus per-skill and
per-toolset sub-tables. Example output (real Hermes install):

```
HERMES CHECKER — STATIC PROMPT SNAPSHOT
============================================================
taken_at:        2026-09-03T06:50:33Z
platform:        cli
model:           qwen3.8-27b:free
provenance:      HERMES_NATIVE_ESTIMATE
hermes_native:   True

TIERS
------------------------------------------------------------
  context    chars=   1,672  bytes=   1,682  tokens_est=    418
  volatile   chars=  15,632  bytes=  16,071  tokens_est=  3,908
  stable     chars=  13,660  bytes=  13,742  tokens_est=  3,415

COMPONENTS
------------------------------------------------------------
  system (sum)     chars=  30,964  tokens_est=  7,741
  tools schemas    chars=  33,899  tokens_est=  8,475
  skills index     chars=  10,982  tokens_est=  2,746
  memory           chars=   2,044  tokens_est=    511
  user profile     chars=   1,332  tokens_est=    333
  ...
```

## Architecture

```
Hermes Agent / Hermes Desktop
            |
            v
   Hermes PluginManager.discover_and_load()
            |
            v
   user-plugin: hermes-checker
     (plugin.yaml + __init__.py)
     registers hooks: pre/post_api_request,
                     pre/post_tool_call,
                     on_session_*, on_skill_lifecycle,
                     subagent_*, ...
            |
            v
   HookCollector (in-process, lazy)
            |
            v
     SQLite (hermes-checker.db, WAL)
            |
   +---------+---------+
   |                   |
   v                   v
CLI report       FastAPI dashboard
                  http://127.0.0.1:8765
                  LIVE / SESSION / ANALYTICS / INSIGHTS
```

The collector is intentionally in-process with Hermes — it never
reaches the network, and the dashboard reads the SQLite file
directly. See `docs/ARCHITECTURE.md` for the full design.

## What it measures

Per API request (`PROVIDER_MEASURED` when the provider reports them,
otherwise `UNAVAILABLE`):

- prompt / input / output / reasoning / cache-read / cache-write /
  total tokens
- cache hit ratio + token-weighted session cache hit
- TTFT (when streaming; else `UNAVAILABLE`)
- tokens-per-second (when streaming)
- P50 / P95 latency, TTFT, TPS
- request duration
- finish reason, model, provider, base_url, api_mode
- payload truncation flag (when Hermes's
  `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` cap fires)
- prompt_visible_chars / tokens_est / confidence (per request, so
  the dashboard can show "we saw X chars of the prompt; the rest
  was truncated")

Per session (locally attributed):

- provider totals
- multi-section prompt-component attribution (`SYSTEM`,
  `TOOLS_SCHEMA`, `SKILLS`, `MEMORY`, `PROJECT_INSTRUCTIONS`,
  `USER_PROFILE`, `MCP_SCHEMAS`, `SUBAGENT_DEFS`, `USER_MESSAGES`,
  `ASSISTANT_HISTORY`, `TOOL_RESULTS`, `OTHER`)
- per-category tool breakdown (`file_read`, `file_write`, `search`,
  `terminal`, `test`, `build`, `git`, `lint`, `web`, `mcp`, `memory`,
  `skill`, `other`) with command-family grouping
- per-(provider, model) performance breakdown
- per-context-size-bucket performance (0-32k / 32-64k / 64-128k /
  128-256k / 256-512k / 512k+)
- LOCALLY_ATTRIBUTED_CONTEXT_DELTA between consecutive requests
  with coverage and per-component contributors
- skill lifecycle facts (loaded / created / patched / archived / …)
- attribution coverage (with explicit un-attributed gap)
- rule-based findings (`POTENTIAL_WASTE` / `HIGH_OVERHEAD` /
  `REPEATED_CONTENT` / `OBSERVATION`) — evidence only, never "waste"
- per-callback self-overhead samples so the user can spot when
  their own plugin becomes the bottleneck

See `docs/METRICS.md` for the full metric dictionary and provenance
rules.

## Privacy & security

V1.1 persists **only** metadata: counts, hashes, character lengths,
timing, classification, error category, exit status. It does **not**
persist full prompts, full responses, source file contents, or
terminal transcripts.

Defense in depth:

- Hermes already redacts the primary secret keys before invoking
  plugins (`api_key`, `authorization`, `bearer ...`, `sk-…`, `pk-…`).
  We re-apply a second-pass secret-pattern sanitizer before write.
- Tool-call **command** lines are stored only as a SHA256 hash + a
  one-word command family (`pytest`, `npm`, `git`, …) + a key list
  (e.g. `["command"]`). The raw command string is never persisted.
- Tool-call **path** strings are stored only as basename + extension +
  a SHA256 hash. The full path is never persisted.
- The dashboard binds to `127.0.0.1` only. It is not exposed on the LAN.
- The `.gitignore` excludes the runtime database, `.env*`, and any
  pricing/profile exports that could contain secrets.

## Experiments and A/B comparisons

Hermes Checker attaches an `experiment` label to every recorded
session. The label is persisted in the SQLite `app_config` table
(Issue 16) and read by the collector on every session start, so
`hermes-checker experiment set baseline-minimax-direct` will apply
the label even when Hermes Desktop is launched without inheriting
the env var.

```powershell
hermes-checker experiment set baseline-minimax-direct
hermes-checker dashboard
```

Suggested labels (you define your own):

- `baseline-minimax-direct` — current free baseline through OpenCode
- `deepseek-openrouter` — same workload, DeepSeek V4 Flash through OpenRouter
- `omniroute-rtk` — same workload, routed through OmniRoute RTK (future)
- `optimized-tools-v1` — same workload, with optimized tool limits (future)

The cost engine reads a user-editable YAML profile (see
`config/pricing.example.yaml`) so you can ask "what would this session
have cost on DeepSeek V4 Flash?" without changing providers.

## Limitations

V1.1 is intentionally lightweight. Known limitations are listed in
`docs/LIMITATIONS.md`. The big ones:

- Component attribution is approximate. Hermes itself does not
  expose per-section token counts. We tokenize the messages Hermes
  shipped and label the output `LOCALLY_ESTIMATED`. When a
  static-prompt snapshot is available, the Hermes-native numbers
  override the local estimate for that component.
- TPS and TTFT only make sense for streaming responses; non-streaming
  responses report them as `UNAVAILABLE`.
- We pin Hermes's hook contract for **agent-side** measurements.
  Anything Hermes doesn't surface stays out of the database —
  we never fabricate.
- `compute_prompt_breakdown` lives inside Hermes Agent's Python. When
  Hermes is on PYTHONPATH (e.g. when running inside the Hermes venv)
  we import it directly; otherwise we spawn a Hermes-Venv Python
  subprocess so users on a separate venv still get native numbers.
- A truncated Hermes payload makes per-component attribution
  unreliable. The collector records `payload_truncated=1` and skips
  attribution for that request.

## Development

```powershell
.venv\Scripts\activate
pytest -q
ruff check .
```

Tests cover schema migrations (v1 → v2 → v3), usage normalization,
cache-hit calculations, pricing, tokenizer fallback, tool
classification (with command-aware sub-classification), attribution
multi-section split, sanitization, report generation, the CLI
parser, the install / uninstall / doctor helpers, **and a real
Hermes `PluginManager` discovery subprocess test** (skipped if Hermes
is not installed on the test machine).

## License

MIT — see `LICENSE`.
