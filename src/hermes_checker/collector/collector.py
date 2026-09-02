"""Collector — turns Hermes hook payloads into Hermes Checker DB rows.

The collector is intentionally stateful so it can correlate events:

- ``pre_api_request`` and ``post_api_request`` carry the same
  ``api_request_id`` (when Hermes supplies one) plus ``api_call_count``.
  We match them in-memory and flush a single :class:`api_requests` row
  on the post hook.
- ``pre_tool_call`` / ``post_tool_call`` match the same way on
  ``tool_call_id``.
- ``on_session_*`` open/close sessions.

The collector also caches a per-session message snapshot so the
``attribute_messages`` pass can compute prompt-component attribution
against the actual messages Hermes shipped, not a guess.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_checker import (
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_PROVIDER_MEASURED,
    PROVENANCE_UNAVAILABLE,
)
from hermes_checker.accounting import (
    ComponentAttribution,
    Tokenizer,
    attribute_messages,
    cache_hit_ratio,
    extract_usage_summary,
    get_tokenizer,
    hash_text,
    sanitize_dict,
    tokens_per_second,
)
from hermes_checker.accounting.attribution import classify_message_role
from hermes_checker.storage import Database, DatabasePaths

from .config import CollectorConfig, default_collector_config

logger = logging.getLogger("hermes_checker.collector")


@dataclass
class _PendingApiRequest:
    """In-flight API request waiting for its post hook."""

    session_id: str
    turn_id: Optional[str]
    api_request_id: Optional[str]
    api_call_count: Optional[int]
    provider: Optional[str]
    model: Optional[str]
    base_url: Optional[str]
    api_mode: Optional[str]
    streaming: Optional[bool]
    started_at: float
    messages_count: Optional[int]
    request_body: Optional[dict[str, Any]] = None
    request_hash: Optional[str] = None


@dataclass
class _PendingToolCall:
    """In-flight tool call waiting for its post hook."""

    session_id: str
    turn_id: Optional[str]
    api_request_row_id: Optional[int]
    tool_call_id: Optional[str]
    tool_name: str
    args_summary: Optional[str]
    started_at: float
    args_hash: Optional[str]


@dataclass
class _SessionState:
    profile: Optional[str] = None
    platform: Optional[str] = None
    started_at: float = field(default_factory=time.time)


class HookCollector:
    """One instance per Hermes backend process. Drop-in target for the user plugin."""

    def __init__(
        self,
        database: Optional[Database] = None,
        config: Optional[CollectorConfig] = None,
        *,
        paths: Optional[DatabasePaths] = None,
    ) -> None:
        self.config = config or default_collector_config()
        if database is not None:
            self.db = database
        else:
            paths = paths or DatabasePaths.default()
            if self.config.database_path:
                paths = DatabasePaths.from_path(self.config.database_path)
            self.db = Database(paths)
        self._pending_api: dict[str, _PendingApiRequest] = {}
        self._pending_tools: dict[str, _PendingToolCall] = {}
        self._sessions: dict[str, _SessionState] = {}
        self._session_request_count: dict[str, int] = {}
        self._tokenizer: Tokenizer = get_tokenizer()
        self._analyzer = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def on_session_start(self, **kwargs: Any) -> None:
        """Hook: ``on_session_start`` (and equivalent entry points)."""
        session_id = str(kwargs.get("session_id") or kwargs.get("chat_id") or "")
        if not session_id:
            return
        profile = kwargs.get("profile") or kwargs.get("platform_profile")
        platform = kwargs.get("platform")
        started_at = float(kwargs.get("started_at") or time.time())
        self._sessions.setdefault(
            session_id,
            _SessionState(profile=profile, platform=platform, started_at=started_at),
        )
        try:
            self.db.upsert_session(
                session_id=session_id,
                profile=profile,
                platform=platform,
                started_at=started_at,
                experiment=self.config.experiment_label,
                metadata=sanitize_dict(kwargs) if kwargs else None,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("on_session_start failed: %s", exc)

    def on_session_end(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        ended_at = float(kwargs.get("ended_at") or time.time())
        try:
            self.db.upsert_session(
                session_id=session_id,
                started_at=self._sessions.get(session_id,
                                              _SessionState()).started_at,
                ended_at=ended_at,
            )
            self.db.insert_event(
                session_id=session_id,
                event_type="session_end",
                payload=sanitize_dict(kwargs),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("on_session_end failed: %s", exc)

    def on_session_finalize(self, **kwargs: Any) -> None:
        self.on_session_end(**kwargs)
        sid = str(kwargs.get("session_id") or "")
        if sid:
            self._sessions.pop(sid, None)
            self._session_request_count.pop(sid, None)

    # ------------------------------------------------------------------
    # API requests
    # ------------------------------------------------------------------

    def pre_api_request(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        api_request_id = str(kwargs.get("api_request_id") or "")
        call_count = kwargs.get("api_call_count")
        # The pair (session_id, api_request_id, api_call_count) is what
        # Hermes uses to correlate. When api_request_id is missing we
        # fall back to (session_id, call_count).
        key = self._api_key(session_id, api_request_id, call_count)
        messages = kwargs.get("messages") or []
        body = kwargs.get("request_body")
        request_hash = hash_text(_flat_messages(messages)) if messages else None

        pending = _PendingApiRequest(
            session_id=session_id,
            turn_id=kwargs.get("turn_id"),
            api_request_id=api_request_id or None,
            api_call_count=call_count,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            base_url=kwargs.get("base_url"),
            api_mode=kwargs.get("api_mode"),
            streaming=bool(kwargs.get("streaming")) if kwargs.get("streaming") is not None else None,
            started_at=float(kwargs.get("started_at") or time.time()),
            messages_count=len(messages) if messages else None,
            request_body=body,
            request_hash=request_hash,
        )
        self._pending_api[key] = pending

        # Cache a sanitized copy of the messages for later attribution.
        if messages:
            self._tokenizer = get_tokenizer(pending.model)
            self._session_messages[session_id] = self._normalise_messages(messages)

    # We lazily allocate the cache so we don't pay for unused sessions.
    @property
    def _session_messages(self) -> dict[str, list[dict[str, Any]]]:
        if not hasattr(self, "_messages_cache"):
            self._messages_cache = {}
        return self._messages_cache

    def _normalise_messages(self, messages: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, Mapping):
                out.append({
                    "role": m.get("role"),
                    "content": m.get("content"),
                    "name": m.get("name"),
                    "tool_call_id": m.get("tool_call_id"),
                })
            else:
                out.append({"role": None, "content": str(m)})
        return out

    def post_api_request(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        api_request_id = str(kwargs.get("api_request_id") or "")
        call_count = kwargs.get("api_call_count")
        key = self._api_key(session_id, api_request_id, call_count)
        pending = self._pending_api.pop(key, None)

        started_at = float(kwargs.get("started_at") or (pending.started_at if pending else time.time()))
        ended_at = float(kwargs.get("ended_at") or time.time())
        duration_s = float(kwargs.get("api_duration") or (ended_at - started_at))
        first_chunk_at = kwargs.get("first_chunk_at")
        ttft_s: Optional[float] = None
        generation_s: Optional[float] = None
        if first_chunk_at is not None:
            try:
                ttft_s = float(first_chunk_at) - started_at
                if ttft_s < 0:
                    ttft_s = None
            except (TypeError, ValueError):
                ttft_s = None
        if ttft_s is not None and duration_s is not None:
            generation_s = max(0.0, duration_s - ttft_s)

        usage = extract_usage_summary(kwargs.get("usage"))
        cache_hit = cache_hit_ratio(usage)
        tps = tokens_per_second(usage, duration_s=generation_s)

        provider = kwargs.get("provider") or (pending.provider if pending else None)
        model = kwargs.get("model") or (pending.model if pending else None)
        response_model = kwargs.get("response_model") or model
        if provider:
            self.db.remember_provider(provider, kwargs.get("base_url"))
        if provider and model:
            self.db.remember_model(provider, model)
        if provider and response_model and response_model != model:
            self.db.remember_model(provider, response_model)

        prompt_prov = usage.prompt_tokens.provenance if usage.prompt_tokens.value is not None else PROVENANCE_UNAVAILABLE
        input_prov = usage.input_tokens.provenance
        output_prov = usage.output_tokens.provenance
        reasoning_prov = usage.reasoning_tokens.provenance
        cache_read_prov = usage.cache_read_tokens.provenance
        cache_write_prov = usage.cache_write_tokens.provenance
        total_prov = usage.total_tokens.provenance

        metadata: dict[str, Any] = {
            "hook": "post_api_request",
            "platform": kwargs.get("platform"),
        }
        if self.config.store_request_payload and pending and pending.request_body is not None:
            metadata["request_body"] = sanitize_dict(
                pending.request_body,
                max_chars=self.config.metadata_max_chars,
            )

        try:
            row_id = self.db.insert_api_request(
                {
                    "session_id": session_id,
                    "turn_id": kwargs.get("turn_id"),
                    "api_request_id": api_request_id or None,
                    "api_call_count": call_count,
                    "provider": provider,
                    "model": model,
                    "base_url": kwargs.get("base_url"),
                    "api_mode": kwargs.get("api_mode"),
                    "streaming": pending.streaming if pending else None,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "first_chunk_at": float(first_chunk_at) if first_chunk_at is not None else None,
                    "duration_s": duration_s,
                    "ttft_s": ttft_s,
                    "finish_reason": kwargs.get("finish_reason"),
                    "response_model": response_model,
                    "messages_count": pending.messages_count if pending else None,
                    "prompt_tokens": usage.prompt_tokens.value,
                    "prompt_tokens_provenance": prompt_prov,
                    "input_tokens": usage.input_tokens.value,
                    "input_tokens_provenance": input_prov,
                    "output_tokens": usage.output_tokens.value,
                    "output_tokens_provenance": output_prov,
                    "reasoning_tokens": usage.reasoning_tokens.value,
                    "reasoning_tokens_provenance": reasoning_prov,
                    "cache_read_tokens": usage.cache_read_tokens.value,
                    "cache_read_tokens_provenance": cache_read_prov,
                    "cache_write_tokens": usage.cache_write_tokens.value,
                    "cache_write_tokens_provenance": cache_write_prov,
                    "total_tokens": usage.total_tokens.value,
                    "total_tokens_provenance": total_prov,
                    "tokens_per_second": tps,
                    "cache_hit_ratio": cache_hit,
                    "raw_usage_json": json.dumps(sanitize_dict(kwargs.get("usage"))) if kwargs.get("usage") else None,
                    "assistant_content_chars": kwargs.get("assistant_content_chars"),
                    "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
                    "request_hash": pending.request_hash if pending else None,
                    "response_hash": hash_text(kwargs.get("assistant_response")) if kwargs.get("assistant_response") else None,
                    "metadata_json": json.dumps(metadata),
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("post_api_request persist failed: %s", exc)
            return

        # Local prompt-component attribution
        try:
            messages = self._session_messages.get(session_id) or []
            if messages:
                components = attribute_messages(messages, get_tokenizer(model))
                self.db.insert_prompt_components(row_id, [c.as_dict() for c in components])
        except Exception as exc:  # pragma: no cover
            logger.warning("attribution failed: %s", exc)

        # Auto-analyze (cheap rule pass) if enabled.
        if self.config.run_analyzer:
            try:
                self._run_analyzer_for_request(row_id, session_id, usage, cache_hit)
            except Exception as exc:  # pragma: no cover
                logger.warning("analyzer failed: %s", exc)

    def api_request_error(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        api_request_id = str(kwargs.get("api_request_id") or "")
        call_count = kwargs.get("api_call_count")
        key = self._api_key(session_id, api_request_id, call_count)
        self._pending_api.pop(key, None)
        try:
            self.db.insert_event(
                session_id=session_id,
                event_type="api_request_error",
                payload=sanitize_dict(kwargs),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("api_request_error failed: %s", exc)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def pre_tool_call(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        if not tool_call_id:
            # Hermes usually supplies one; if not, we just skip — there's
            # nothing to match against.
            return
        args = kwargs.get("args") or {}
        args_hash = hash_text(_safe_json(args))
        args_summary = _summarize_args(args)
        pending = _PendingToolCall(
            session_id=session_id,
            turn_id=kwargs.get("turn_id"),
            api_request_row_id=None,  # we don't have a stable link here
            tool_call_id=tool_call_id,
            tool_name=str(kwargs.get("tool_name") or ""),
            args_summary=args_summary,
            started_at=float(kwargs.get("started_at") or time.time()),
            args_hash=args_hash,
        )
        self._pending_tools[(session_id, tool_call_id)] = pending

    def post_tool_call(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        if not session_id:
            return
        pending = self._pending_tools.pop((session_id, tool_call_id), None)

        started_at = float(kwargs.get("started_at") or (pending.started_at if pending else time.time()))
        ended_at = float(kwargs.get("ended_at") or time.time())
        duration_ms = float(kwargs.get("duration_ms") or ((ended_at - started_at) * 1000.0))

        result = kwargs.get("result")
        result_str = _result_to_text(result)
        output_chars = len(result_str)
        output_tokens_est = max(1, (output_chars + 3) // 4) if output_chars else 0
        output_hash = hash_text(result_str)
        output_truncated = _looks_truncated(result_str)

        tool_name = kwargs.get("tool_name") or (pending.tool_name if pending else "")
        category = classify_tool(str(tool_name))
        args_summary = pending.args_summary if pending else _summarize_args(kwargs.get("args") or {})

        try:
            self.db.insert_tool_call(
                {
                    "session_id": session_id,
                    "turn_id": kwargs.get("turn_id") or (pending.turn_id if pending else None),
                    "api_request_row_id": pending.api_request_row_id if pending else None,
                    "tool_call_id": tool_call_id or None,
                    "tool_name": tool_name,
                    "category": category,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_ms": duration_ms,
                    "status": kwargs.get("status"),
                    "error_type": kwargs.get("error_type"),
                    "error_message": kwargs.get("error_message"),
                    "exit_code": kwargs.get("exit_code"),
                    "output_truncated": output_truncated,
                    "input_chars": None,
                    "input_tokens_est": None,
                    "output_chars": output_chars,
                    "output_tokens_est": output_tokens_est,
                    "output_hash": output_hash,
                    "args_hash": pending.args_hash if pending else hash_text(_safe_json(kwargs.get("args"))),
                    "args_summary": args_summary,
                    "metadata_json": None,
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("post_tool_call persist failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _api_key(session_id: str, api_request_id: str, call_count: Any) -> str:
        if api_request_id:
            return f"{session_id}::{api_request_id}"
        return f"{session_id}::count::{call_count}"

    def _run_analyzer_for_request(
        self,
        api_request_row_id: int,
        session_id: str,
        usage: Any,
        cache_hit: Optional[float],
    ) -> None:
        # Lazy import to keep the cold path cheap
        if self._analyzer is None:
            from hermes_checker.analysis import Analyzer
            self._analyzer = Analyzer(self.db, self.config)
        self._analyzer.analyze_request(
            api_request_row_id=api_request_row_id,
            session_id=session_id,
            usage=usage,
            cache_hit_ratio=cache_hit,
        )


# ---------------------------------------------------------------------------
# Pure helpers (tested directly in tests/test_accounting.py etc.)
# ---------------------------------------------------------------------------


def classify_tool(tool_name: str) -> str:
    """Bucket a tool name into one of our coarse categories."""
    if not tool_name:
        return "other"
    name = tool_name.lower()
    # Order matters: more specific (and overlapping) patterns come first.
    mapping = (
        ("file_read", ("read_file", "read", "cat", "view", "load_file", "file_read", "open_file")),
        ("file_write", ("write_file", "edit_file", "create_file", "file_write", "file_edit", "patch", "apply_patch")),
        ("terminal", ("terminal", "bash", "shell", "execute_command", "run_command", "subprocess", "exec")),
        ("test", ("pytest", "test", "unittest", "jest", "playwright", "mocha", "vitest")),
        ("build", ("build", "compile", "make", "npm run build", "docker build", "cargo build")),
        ("git", ("git", "commit", "diff", "pr", "pull_request", "git_diff")),
        ("web", ("web_search", "web_fetch", "http", "fetch", "curl", "scrape", "brave_search")),
        ("search", ("search", "grep", "ripgrep", "find_files", "code_search", "glob", "rg")),
        ("mcp", ("mcp_", "_mcp")),
        ("memory", ("memory", "recall", "remember", "forget")),
        ("skill", ("skill", "load_skill", "invoke_skill")),
    )
    for category, needles in mapping:
        for n in needles:
            if name == n or name.startswith(n + "_") or n in name:
                return category
    return "other"


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        return str(value)


def _summarize_args(args: Any, *, max_len: int = 200) -> Optional[str]:
    if args is None:
        return None
    try:
        s = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        s = str(args)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _result_to_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


def _looks_truncated(text: str) -> Optional[bool]:
    if not text:
        return None
    markers = ("[truncated", "...truncated", "[output truncated", "<truncated",
               "result truncated", "elided")
    lower = text.lower()
    return any(m in lower for m in markers)


def _flat_messages(messages: Any) -> str:
    """Concatenate all message contents into one big string for hashing."""
    parts: list[str] = []
    for m in messages or []:
        if isinstance(m, Mapping):
            role = m.get("role")
            content = m.get("content")
            parts.append(f"[{role}] {_content_to_text(content)[:1500]}")
    return "\n".join(parts)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                parts.append(str(part.get("text", part)))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    if isinstance(content, Mapping):
        if "text" in content:
            return str(content.get("text", ""))
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)
    return str(content)


# Backwards-compatible aliases
Collector = HookCollector