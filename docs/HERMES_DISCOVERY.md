# Hermes Discovery — Phase 0 Findings

This document captures what Hermes Checker discovered about the user's
Hermes installation on this Windows machine before any code was written.

## 1. Installations Located

### Hermes Desktop (Electron GUI)

- Executable: `C:\Users\xampos\AppData\Local\hermes\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe`
- Version string (`resources/version`): `40.10.2`
- Build stamp (`resources/app.asar.unpacked/../install-stamp.json`):
  - commit `0ee98eda521cd6f0f0d9bd7420cb66a998b15a8a`
  - branch `main`
  - built `2026-09-02T16:51:10.866Z`
- Renderer is a packaged React/Vite SPA inside `resources/app.asar`
  (~8.9 MB compressed, with `app.asar.unpacked/` for native helpers).
- Frontend connects to a local WebSocket / JSON-RPC gateway. The renderer
  contains a `JsonRpcGatewayError` class and a WebSocket client with
  heartbeats, replay buffers, and event handlers
  (`dist/assets/hermes-BbzFxon3.js` and `chat-runtime-Bd6_rVba.js`).

### Hermes Agent backend (Python)

The Electron app does **not** run any agent logic itself. It spawns a
Python backend per active profile. From
`%APPDATA%\hermes\backend-ownership.json`:

```
C:\Users\xampos\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe
  -m hermes_cli.main --profile <name> serve --host 127.0.0.1 --port 0
```

The backend binds to a random localhost port. The Electron renderer
discovers the port via `backend-ownership.json` and connects over
WebSocket.

`hermes_home()` returns `C:\Users\xampos\AppData\Local\hermes` (NOT
`~/.hermes`). All user-facing config / data / hooks / plugins live under
that directory.

### Separate, unrelated "HERMES" project

`C:\Users\xampos\HERMES\` contains a different Python project
(`hermes_agent.py`) — a local Sysmon-event triage pipeline that uses
DeepSeek for LLM escalation. This project is **not** the same as the
Hermes Agent / Hermes Desktop installed in `C:\Users\xampos\AppData\Local\hermes\hermes-agent\`.
Hermes Checker targets the NousResearch Hermes Agent stack; we ignore
the unrelated project.

## 2. Hermes Agent Source Layout (for reference)

`C:\Users\xampos\AppData\Local\hermes\hermes-agent\` is a clone of the
NousResearch/hermes-agent repository. Relevant directories:

```
hermes_cli/
  subcommands/        # `hermes <sub>` implementations
  observability/      # Hermes shared-metrics (Hermes-side telemetry)
  plugins.py          # Plugin discovery + hook firing
  hooks.py            # Hook contract schemas / tests
  lifecycle.py        # invoke_hook / has_hook helpers
agent/
  conversation_loop.py  # Calls invoke_hook("post_api_request", ...)
  run_agent.py           # _usage_summary_for_api_request_hook
  usage_pricing.py       # CanonicalUsage dataclass + PricingEntry
gateway/
  hooks.py            # Simpler, older hook system (NOT what we use)
  run.py              # Gateway server
plugins/                # Bundled plugins (langfuse, etc.)
hermes_state.py       # Session/usage SQL persistence
```

## 3. The Officially Supported Plugin / Hook System

Hermes Agent ships a **first-class plugin system** with per-API-call and
per-tool-call hooks. This is the integration surface we will use.

### Hook taxonomy (canonical, current)

Source: `hermes_cli/plugins.py` `VALID_HOOKS`, plus the actual call sites
in `agent/conversation_loop.py` and `model_tools.py`.

| Hook                  | Fires when…                                       | Payload (selected kwargs)                                                                                                                                                                  |
|-----------------------|---------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `pre_api_request`     | Just before an HTTP call to the LLM provider      | `task_id`, `session_id`, `turn_id`, `api_request_id`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `started_at`, `messages`, `request_body`, `streaming`               |
| `post_api_request`    | After the LLM responds                             | `task_id`, `turn_id`, `api_request_id`, `session_id`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `api_duration`, `started_at`, `ended_at`, `first_chunk_at`, `finish_reason`, `response_model`, `response`, `usage`, `assistant_message`, `assistant_content_chars`, `assistant_tool_call_count`, `moa_references` |
| `api_request_error`   | When an HTTP call to the LLM fails                | `provider`, `model`, `status_code`, `error_type`, `error_code`, `error_message`, etc.                                                                                                    |
| `pre_llm_call`        | Once per turn, before the conversation loop runs   | `task_id`, `session_id`, `model`, `provider`, `messages`, …                                                                                                                                  |
| `post_llm_call`       | Once per turn, after the assistant finishes       | `task_id`, `session_id`, `model`, `provider`, `assistant_message`, `response`, …                                                                                                            |
| `pre_tool_call`       | Just before a tool is executed                    | `tool_name`, `args`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`                                                                                                |
| `post_tool_call`      | After a tool finishes                             | `tool_name`, `args`, `result`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message`, `middleware_trace`         |
| `on_session_start`    | A session is created                              | `session_id`, `profile`, …                                                                                                                                                                  |
| `on_session_end`      | A session is closed normally                      | `session_id`, `reason`, …                                                                                                                                                                    |
| `on_session_finalize` | Hard session boundary (`/new`, `/reset`, exit)    | `session_id`, `reason`, …                                                                                                                                                                    |
| `on_session_reset`    | After `/reset`                                    | `session_id`, `reason`, …                                                                                                                                                                    |
| `subagent_start` / `subagent_stop` | Subagent lifecycle                | `subagent_id`, `parent_session_id`, `task`, …                                                                                                                                               |
| `on_skill_lifecycle`  | Skill loaded/used/unloaded                        | `skill_name`, `event`, `session_id`, …                                                                                                                                                       |

Crucially, `post_api_request` carries a fully populated `usage` dict
already normalized into Hermes' `CanonicalUsage` shape, plus
`first_chunk_at` and `api_duration`, which gives us provider-measured
TTFT and TPS without any client-side polling.

### Hook payload sanitization

Hermes already redacts `api_key`, `authorization`, `proxy_authorization`,
`cookie`, `set_cookie`, and any `*_api_key` field in hook payloads
(`_is_sensitive_hook_key` in `run_agent.py`). Hermes Checker's own
collection layer re-applies a second, stricter pass before persisting.

### Where plugins live

Bundled plugins ship at `<repo>/plugins/<name>/{plugin.yaml,__init__.py}`.
User plugins are scanned from `get_hermes_home() / "plugins" / <name>/`
(typically `C:\Users\xampos\AppData\Local\hermes\plugins\<name>\`).

### How plugins are enabled

`hermes_cli/plugins.py` reads `plugins.enabled` from `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-checker
```

The CLI subcommand `hermes plugins enable <name>` writes this for the
user. Hermes Checker ships as a user plugin named `hermes-checker`.

### Reference: `plugins/observability/langfuse/__init__.py`

The bundled Langfuse plugin already registers `pre_api_request`,
`post_api_request`, `api_request_error`, `pre_llm_call`,
`post_llm_call`, `pre_tool_call`, `post_tool_call`,
`on_session_finalize`, `on_session_end`, `subagent_start`,
`subagent_stop`. This is the exact register pattern we mirror.

## 4. `CanonicalUsage` shape (what Hermes gives us for free)

From `agent/usage_pricing.py`:

```python
@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw_usage: Optional[dict[str, Any]] = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens
```

The `usage` kwarg on `post_api_request` is `asdict(cu)` minus
`raw_usage` plus `prompt_tokens` and `total_tokens`. So a single
`post_api_request` payload contains every per-API-call token field the
spec asked for.

Provider-measured fields available (when the underlying provider
returns them): `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, plus Hermes-computed
`prompt_tokens` and `total_tokens`. Anything else is left out and must
be flagged as `LOCALLY_ESTIMATED`.

## 5. Prompt Component Attribution

Hermes does NOT expose per-component token counts (no per-section
usage breakdown on its own). `agent/turn_context.py` does build a
`TurnContext` with per-source contributions (memory, prefetch,
`pre_llm_call` plugin context) but only as character counts, not tokens.

**Strategy for V1:**

- Provider-measured fields (input/output/cache read/write/reasoning,
  total) come from `CanonicalUsage` via `post_api_request`.
- Component attribution is computed LOCALLY from the rendered messages
  Hermes actually sends. Hermes sanitizes payloads before handing them
  to plugins, but the `messages` kwarg on `pre_api_request` (and
  `pre_llm_call`) is the JSON the agent just shipped. We tokenize each
  message and attribute by role / tool_call_id / system-style prefix.
- The CLI exposes BOTH the provider total and the local attribution
  total, plus a labeled `Attribution error = local - provider` so users
  can see the unexplained delta.

## 6. Hermes Desktop Specifics

- Hermes Desktop does NOT change the integration path: it just hosts the
  WebSocket connection to the Python backend. Every desktop turn goes
  through the same Python `serve` process that a CLI turn would.
- Therefore enabling the `hermes-checker` user plugin makes the desktop
  observe itself automatically. No Electron-side patching is needed.
- The only Desktop-side requirement: **restart Hermes Desktop** (which
  restarts its Python backend) after enabling the plugin.

## 7. Tool Classification

Tool names are exposed verbatim on `post_tool_call`. We classify into:

| Category     | Pattern matched against `tool_name`                                                                              |
|--------------|------------------------------------------------------------------------------------------------------------------|
| `file_read`  | `read_file`, `read`, `cat`, `view`, `load_file`, `file_read`                                                     |
| `file_write` | `write_file`, `edit_file`, `create_file`, `file_write`, `file_edit`, `patch`                                      |
| `search`     | `search`, `grep`, `ripgrep`, `find_files`, `code_search`, `glob`                                                 |
| `terminal`   | `terminal`, `bash`, `shell`, `execute_command`, `run_command`, `subprocess`                                       |
| `test`       | `pytest`, `test`, `unittest`, `jest`, `playwright`, `mocha`                                                       |
| `build`      | `build`, `compile`, `make`, `npm run build`, `docker build`, `cargo build`                                        |
| `git`        | `git`, `commit`, `diff`, `pr`, `pull_request`                                                                     |
| `web`        | `web_search`, `web_fetch`, `http`, `fetch`, `curl`, `scrape`, `brave_search`                                      |
| `mcp`        | `mcp_*`, `*_mcp`, names starting with the configured MCP server prefixes                                          |
| `memory`     | `memory`, `recall`, `remember`, `forget`                                                                          |
| `skill`      | `skill`, `load_skill`, `invoke_skill`                                                                              |
| `other`      | Everything else                                                                                                   |

This is intentionally a heuristic. Hermes Checker does NOT pretend the
classification is provider-measured.

## 8. Privacy and Secret Handling

- V1 never persists: full prompts, full tool outputs, source file
  contents, or any other potentially proprietary content.
- We persist: counts, character lengths, hashes (SHA256), timing,
  classification, error category, exit status, sanitized metadata.
- Defense in depth: a second secret-pattern sanitizer runs on every
  field we are about to write, even though Hermes already redacts the
  primary secret keys.

## 9. Limitations

| Limitation | Reason |
|---|---|
| Component attribution is approximate | Hermes does not expose per-section token counts; we tokenize the messages Hermes just sent. Local tokenizer is `cl100k_base` (tiktoken) if installed, else a `chars/4` fallback. Always labeled `LOCALLY_ESTIMATED`. |
| TPS can be derived only when streaming | TTFT and tokens/sec require `first_chunk_at` + `output_tokens`; non-streaming responses leave these fields None and Hermes Checker reports them as unavailable. |
| `output_tokens` may include reasoning_tokens | Provider-specific. We display reasoning separately when provided and label the overlap explicitly. |
| Pre-API-request prompts may be massive | Hermes truncates hook payloads at `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` (default 50 000). We persist only the hash + length + per-message attribution, never the raw prompt. |
| Hermes version skew | Hook payloads evolve additively; we accept `**kwargs` and tolerate missing fields. |

## 10. Integration Strategy Chosen

**User plugin** at `~/.hermes/plugins/hermes-checker/`, enabled via
`plugins.enabled: [hermes-checker]` in `~/.hermes/config.yaml`.

This:

- Requires NO modification of Hermes itself.
- Loads automatically when Hermes Desktop / CLI / TUI start the Python
  backend.
- Stays in sync with future Hermes versions — Hermes's plugin system
  is its stable integration contract.
- Can be disabled by removing the entry from `plugins.enabled`.

The `hermes-checker install` CLI subcommand copies the plugin into
place and edits `config.yaml` (with a backup, atomically) to add it
under `plugins.enabled`.

## 11. Fallback Strategy

If for any reason the user plugin path is unavailable (custom Hermes
build, hooks disabled, plugin not loaded), the doctor command reports
the situation and prints a manual recovery. We do NOT implement a
secondary passive observer in V1 because it would either re-implement
hook firing or parse logs — both are invasive in different ways.