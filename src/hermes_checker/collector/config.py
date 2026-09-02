"""Runtime configuration for the collector.

Most knobs live in :class:`CollectorConfig`.  The defaults are tuned
for non-invasiveness: the collector should add no measurable latency
to Hermes's hot path and should never write full content to the
database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectorConfig:
    """Tunables for the collector.  Pass an instance to :class:`HookCollector`."""

    # Where to write the SQLite database. ``None`` → use
    # :func:`hermes_checker.storage.DatabasePaths.default`.
    database_path: str | None = None

    # Experiment label attached to every new session.  Used to keep
    # multiple A/B runs cleanly separated (e.g. "baseline-minimax-direct",
    # "deepseek-openrouter").
    experiment_label: str | None = None

    # If True, the collector will run the rule-based analyzer on every
    # recorded API request.  Default False because the analyzer is
    # cheap but not free and many users just want raw observations.
    run_analyzer: bool = False

    # Threshold (tokens) above which the analyzer flags a "large context jump".
    large_jump_tokens: int = 20_000

    # Threshold (chars) above which the analyzer flags a tool output
    # as "potentially wasteful".
    large_tool_output_chars: int = 20_000

    # Cache-hit ratio below which the analyzer flags the request as
    # "cache miss burst".
    cache_hit_floor: float = 0.5

    # Maximum characters we'll keep in any one metadata field.
    metadata_max_chars: int = 8_000

    # If True, the collector records the sanitized payload dict from
    # ``post_api_request`` (excluding heavy fields like the assistant
    # message). Off by default to keep the DB small; turn it on if you
    # want deeper debugging.
    store_request_payload: bool = False


def default_collector_config() -> CollectorConfig:
    """Build a :class:`CollectorConfig` from environment overrides."""
    return CollectorConfig(
        database_path=os.environ.get("HERMES_CHECKER_DB"),
        experiment_label=os.environ.get("HERMES_CHECKER_EXPERIMENT"),
        run_analyzer=os.environ.get("HERMES_CHECKER_ANALYZER", "").lower()
        in ("1", "true", "yes", "on"),
        large_jump_tokens=int(os.environ.get("HERMES_CHECKER_LARGE_JUMP_TOKENS", "20000")),
        large_tool_output_chars=int(os.environ.get("HERMES_CHECKER_LARGE_TOOL_CHARS", "20000")),
        cache_hit_floor=float(os.environ.get("HERMES_CHECKER_CACHE_FLOOR", "0.5")),
        metadata_max_chars=int(os.environ.get("HERMES_CHECKER_METADATA_CHARS", "8000")),
        store_request_payload=os.environ.get("HERMES_CHECKER_STORE_PAYLOAD", "").lower()
        in ("1", "true", "yes", "on"),
    )


__all__ = ["CollectorConfig", "default_collector_config"]