# Hermes Checker — Limitations

V1 is intentionally lightweight. Below is the honest list of
things it does NOT do, what we recommend instead, and what is on
the roadmap for V2.

## Observational limitations

### 1. Component attribution is approximate

Hermes does not expose per-section token counts (no
"system used X tokens, tools used Y tokens, …" breakdown on its
own). Hermes Checker approximates this by tokenising the
`messages` payload that Hermes is about to send to the provider.
This is the most accurate approach available without modifying
Hermes itself, but it is still local attribution.

- The result is always labelled `LOCALLY_ESTIMATED`.
- The CLI report shows `Attribution error vs provider` so the user
  can see the unexplained gap between our sum and the
  provider-reported prompt tokens.
- When the optional `tiktoken` dep is not installed, tokenization
  uses a `chars/4` heuristic. Even with tiktoken, the encoder we
  pick may not match the provider's exact tokenizer (Hermes's
  default is `cl100k_base`).

**Recommendation:** treat the breakdown as a relative ranking
("file reads dominate" is more trustworthy than "tools used exactly
9 742 tokens"), and use the provider-measured `prompt_tokens` as the
ground truth for any cost or budgeting decision.

### 2. TPS / TTFT only for streaming

We rely on the `first_chunk_at` timestamp from Hermes's
`post_api_request` hook. If a request is non-streaming (or the
provider doesn't surface this signal), both TTFT and TPS are
`UNAVAILABLE`. We never guess.

### 3. Provider cost is not stored

We do not persist `provider_cost_usd` from the provider's invoice
because (a) the Hermes hook payload doesn't include it, and (b)
the spec's privacy rules say "no raw responses". The CLI
`PROJECTED COST` section is computed against a user-editable YAML
profile.

**Workaround for cost accuracy:** switch providers' requests
through an intermediary (such as OpenRouter, LiteLLM, or a custom
gateway) and use that intermediary's logged cost as a comparison
input. The database schema already separates Hermes-side tokens
from projected cost so an A/B against a gateway is straightforward.

### 4. `output_tokens` may include `reasoning_tokens`

Some providers (Anthropic, OpenAI o-series) include the reasoning
content in the visible output. We always display the provider's
`output_tokens` as-is and surface `reasoning_tokens` separately
when the provider reports it. If both are non-null, the report
explicitly says so.

### 5. Prompt payloads can be truncated

Hermes sanitizes hook payloads at `HERMES_PLUGIN_PAYLOAD_MAX_CHARS`
(default 50 000). For a 350 000-token session this means a
`pre_api_request` may not see the full message history — only a
truncated copy. We always persist a SHA256 of the visible payload
so duplicate detection still works on the same Hermes process; if
Hermes chunks a payload we will see different hashes for adjacent
identical messages, which the analyzer will flag as a heuristic
"repeated content" finding.

## Integration limitations

### 6. Hermes must be running with the plugin system enabled

Hermes Checker is a user plugin. It will not load if:

- The Hermes build has the plugin system disabled
- The `~/.hermes/plugins/` directory has been removed or made
  read-only
- The `plugins.enabled` config entry was removed
- The Python environment Hermes uses is missing
  `hermes_checker` (the install command copies it next to the
  plugin, so this is rare)

The `doctor` command reports any of these situations and prints
manual recovery steps.

### 7. Hermes Desktop needs a restart after install

Hermes Desktop spawns a fresh Python backend on launch. Until you
restart it, the new plugin is not loaded. The `install` command
prints the restart reminder.

### 8. We don't track MCP tool results in detail

MCP tool calls show up in `tool_calls` like any other tool, but we
don't decode the MCP envelope (`mcp_server`, `tool_name`, etc.).
The category heuristic is good enough for A/B; if you need the
exact MCP server name per call, the `tool_name` field carries it
verbatim.

## Future work (V2 candidates)

These were considered but deferred to keep V1 small.

| Feature                            | Reason deferred                                |
|------------------------------------|------------------------------------------------|
| Embedding-based prompt similarity   | V1 is metadata-only; embeddings would violate  |
|                                    | "no huge dependencies" rule                    |
| Auto-experimentation (load-balance)| Touches routing — out of scope                  |
| Browser/desktop UI hooks           | Already covered by the CLI + FastAPI dashboard |
| Cross-session token budgets        | Requires writing changes to Hermes (forbidden) |
| Real-time WebSocket push to the UI  | The dashboard polls at 5s; a future PR can     |
|                                    | add SSE if needed                              |
| Provider-issued cost capture       | Requires storing more of the response payload |
| Webhook / Slack / email reporting  | Out of scope for V1                            |

## Compatibility matrix

| Component                  | Version tested        |
|----------------------------|-----------------------|
| Hermes Agent (Python)      | current `main`         |
| Hermes Desktop (Electron) | 40.10.2                |
| Python                     | 3.10 / 3.11 / 3.12    |
| SQLite                     | shipped with Python 3.x |
| FastAPI (optional)         | 0.110+                 |
| tiktoken (optional)        | 0.6+                  |
| PyYAML (optional)          | 6.0+                  |

The plugin's `register(ctx)` uses the `ctx.register_hook(name, fn)`
contract. Older Hermes versions that predate the hook system are
not supported.