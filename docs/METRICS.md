# Hermes Checker — Metric Dictionary

Every metric in Hermes Checker carries an explicit provenance tag
(``PROVIDER_MEASURED`` / ``HERMES_MEASURED`` / ``LOCALLY_CALCULATED`` /
``LOCALLY_ESTIMATED`` / ``UNAVAILABLE``). This document lists every
metric we record, where it comes from, and where it shows up in the
UI/CLI.

## Tokens (per API request)

| Metric             | Provenance          | Source                                           |
|--------------------|---------------------|--------------------------------------------------|
| `prompt_tokens`    | LOCALLY_CALCULATED  | `input_tokens + cache_read + cache_write`        |
| `input_tokens`     | PROVIDER_MEASURED   | `usage.input_tokens` from Hermes hook            |
| `output_tokens`    | PROVIDER_MEASURED   | `usage.output_tokens` from Hermes hook           |
| `reasoning_tokens` | PROVIDER_MEASURED   | `usage.reasoning_tokens` when the provider reports it |
| `cache_read_tokens`| PROVIDER_MEASURED   | `usage.cache_read_tokens` from Hermes hook       |
| `cache_write_tokens`| PROVIDER_MEASURED  | `usage.cache_write_tokens` from Hermes hook      |
| `total_tokens`     | LOCALLY_CALCULATED  | `prompt_tokens + output_tokens`                  |

When a provider doesn't report a bucket, we leave it as
``UNAVAILABLE`` rather than fabricate it. The dashboard / report
renders the value as `—` in that case.

Hermes exposes the canonical field names from
`agent.usage_pricing.CanonicalUsage` (`input_tokens`,
`output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`reasoning_tokens`) and a few provider-specific aliases
(`prompt_tokens`, `completion_tokens`, `cached_tokens`,
`thought_tokens`). We accept all of them.

## Latency / performance (per API request)

| Metric             | Provenance          | Definition                                        |
|--------------------|---------------------|---------------------------------------------------|
| `duration_s`       | PROVIDER_MEASURED   | `ended_at - started_at` from Hermes hook          |
| `ttft_s`           | PROVIDER_MEASURED   | `first_chunk_at - started_at` (streaming only)    |
| `tokens_per_second`| LOCALLY_CALCULATED  | `output_tokens / (duration_s - ttft_s)`           |
| `cache_hit_ratio`  | LOCALLY_CALCULATED  | `cache_read_tokens / prompt_tokens`               |
| `streaming`        | PROVIDER_MEASURED   | from `streaming` kwarg on the pre hook            |
| `finish_reason`    | PROVIDER_MEASURED   | `usage`/response finish reason                    |

When `first_chunk_at` is `None` (non-streaming response), `ttft_s`
and `tokens_per_second` are also `None`. The UI/CLI render them as
`UNAVAILABLE`, never as a guessed number.

## Context component attribution (per API request)

| Component                | Meaning                                                         |
|--------------------------|-----------------------------------------------------------------|
| `SYSTEM`                 | role=system messages                                           |
| `TOOLS_SCHEMA`           | role=system messages whose content looks like a tool schema     |
| `SKILLS`                 | skill bundle markers in role=user/system messages               |
| `MEMORY`                 | role=user messages with explicit memory framing                |
| `PROJECT_INSTRUCTIONS`   | role=system messages that begin with AGENTS.md style headers    |
| `USER_MESSAGES`          | role=user, not classified as memory / skills                    |
| `ASSISTANT_HISTORY`      | role=assistant messages                                         |
| `TOOL_RESULTS`            | role=tool messages                                              |
| `OTHER`                  | everything we can't classify                                    |

Every component row carries:

- `characters` — character count
- `bytes` — UTF-8 byte count
- `estimated_tokens` — tokenized locally
- `measurement_method` — `TIKTOKEN` (when the optional dep is
  installed) or `HEURISTIC` (chars/4 fallback)
- `confidence` — 0..1
- `source_identifier` — short hash of the first content we saw

All numbers in this table are `LOCALLY_ESTIMATED`. The
`Attribution error vs provider` line in the CLI report shows the
delta between our sum and the provider-reported prompt tokens, so
the user can see the unexplained gap.

## Tool profiling (per tool call)

| Field                | Provenance        | Source                                |
|----------------------|-------------------|---------------------------------------|
| `tool_name`          | PROVIDER_MEASURED | from the `tool_name` kwarg             |
| `category`           | LOCALLY_ESTIMATED | heuristic name match                   |
| `duration_ms`        | PROVIDER_MEASURED | from the `duration_ms` kwarg          |
| `status`             | PROVIDER_MEASURED | ok / error / needs-auth / timeout     |
| `error_type`         | PROVIDER_MEASURED | classification from Hermes             |
| `exit_code`          | PROVIDER_MEASURED | from tool wrapper                      |
| `output_chars`       | LOCALLY_ESTIMATED | length of the result text             |
| `output_tokens_est`  | LOCALLY_ESTIMATED | chars/4 fallback                      |
| `output_hash`        | LOCALLY_ESTIMATED | SHA256 of the result text             |
| `args_hash`          | LOCALLY_ESTIMATED | SHA256 of the JSON-serialised args   |
| `args_summary`       | LOCALLY_ESTIMATED | truncated JSON of the args            |
| `output_truncated`   | LOCALLY_ESTIMATED | True/False/UNKNOWN                    |

We never persist the full args or result text — only truncated
summaries and hashes. The args summary is bounded to ~200 chars.

## Cost (per session, on demand)

| Field                  | Provenance        | Source                                |
|------------------------|-------------------|---------------------------------------|
| `provider_cost_usd`    | UNAVAILABLE       | we do not store provider invoices     |
| `projected_cost_usd`   | LOCALLY_CALCULATED| computed against a user-editable YAML |
| `projected_cost_profile` | —              | which profile was used                |

Cost is computed against a profile in
`config/pricing.example.yaml` (or a user-edited copy). Each profile
has per-million-token rates for `input`, `cached_input`,
`cache_write`, `output`, and `reasoning`. Missing rates are treated
as zero — we never fabricate prices.

## Findings (rule-based)

Every finding row carries:

- `finding_kind` — machine label (e.g. `large_context_jump`,
  `cache_miss_burst`, `large_terminal_outputs`,
  `tool_category_dominance`, `repeated_tool_output`)
- `severity` — `POTENTIAL_WASTE` / `HIGH_OVERHEAD` /
  `REPEATED_CONTENT` / `OBSERVATION`
- `confidence` — 0..1
- `evidence_json` — the exact numbers that triggered the rule
- `message` — short human-readable text
- `detected_at` — when the rule fired

We do NOT call anything "waste" definitively. Even `POTENTIAL_WASTE`
findings come with evidence so the user can verify them.

## Per-session aggregates (CLI / SESSION view)

| Aggregate                          | Definition                                  |
|------------------------------------|---------------------------------------------|
| `prompt_tokens_total`              | sum across `prompt_tokens`                   |
| `cache_read_tokens_total`          | sum across `cache_read_tokens`               |
| `fresh_tokens`                     | `prompt_tokens_total - cache_read_total`     |
| `cache_hit_ratio_avg`              | mean of per-request `cache_hit_ratio`        |
| `tps_avg`                          | mean of per-request `tokens_per_second`      |
| `ttft_avg`                         | mean of per-request `ttft_s`                  |
| `duration_s_api_avg`               | mean of per-request `duration_s`             |
| `streaming_requests`               | count of requests with `streaming=1`         |

## Where each metric shows up

| View         | Aggregates                                            |
|--------------|-------------------------------------------------------|
| LIVE         | latest session, latest totals, last 10 events         |
| SESSION      | full per-request + per-component + per-tool timeline  |
| ANALYTICS    | windowed aggregates over all sessions                 |
| INSIGHTS     | the most recent rule-based findings                    |

## Sanitization guarantee

Every value written to the database passes through
`hermes_checker.accounting.sanitize` (a second-pass scrub on top
of Hermes's own redaction). The patterns we look for:

- Anthropic-style `sk-ant-…`
- OpenAI / OpenRouter style `sk-…` and `pk-…`
- JWT (`eyJ…`)
- AWS access key (`AKIA…`)
- GitHub PAT (`ghp_…` / `github_pat_…`)
- Slack tokens (`xox[abprs]-…`)
- Authorization / Bearer headers
- `api_key=value` (case-insensitive)

Replacements are always `<redacted>`. We do not log redactions.