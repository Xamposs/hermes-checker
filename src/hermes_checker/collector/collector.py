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
    # Raw messages from the pre hook, kept so the post hook can
    # tokenise the visible payload to detect truncation and attribute
    # prompt components.
    messages: Optional[list[Any]] = None


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
    # Issue 9 + Issue 11: we keep the raw args so post_tool_call can
    # build the command family / command hash / path summary.  We never
    # persist the raw args directly; only derived fields and a hash.
    args: Optional[dict[str, Any]] = None


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
        # (row_id, prompt_tokens) for the most recent api_requests row
        # we observed, per session. Used by the context-delta attribution
        # pass to pair the current request with the previous one.
        self._last_request_cache: dict[str, tuple[int, Optional[int]]] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _record_self_overhead(self, callback_name: str, started_at: float) -> None:
        """Issue 19: record how long one of our own callbacks took.

        Cheap (one INSERT, sampled) and entirely off the hot path.  We
        surface a warning when a single sample exceeds 50ms so the
        user can notice when their own plugin becomes the perf
        bottleneck.
        """
        duration_ms = (time.time() - started_at) * 1000.0
        try:
            self.db.record_self_overhead(callback_name, duration_ms)
        except Exception as exc:  # pragma: no cover
            logger.debug("self-overhead record failed: %s", exc)
        if duration_ms > 50.0:
            logger.warning(
                "hermes_checker: %s took %.1fms (>50ms); "
                "consider reducing the work this callback does",
                callback_name, duration_ms,
            )

    def _timed(self, callback_name: str):
        """Context manager: time the wrapped block, log + record.

        Usage::

            with self._timed("post_api_request"):
                ... do the work ...
        """
        outer = self
        class _Timer:
            def __enter__(self):
                self._t = time.time()
                return self
            def __exit__(self, exc_type, exc, tb):
                outer._record_self_overhead(callback_name, self._t)
        return _Timer()

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

    def on_session_reset(self, **kwargs: Any) -> None:
        """Hook: a session was reset (typically /reset).  Treat it as a
        session-end with the same persistence; clear in-flight caches.
        """
        try:
            self.db.insert_event(
                session_id=str(kwargs.get("session_id") or "") or None,
                event_type="session_reset",
                payload=sanitize_dict(kwargs),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("on_session_reset failed: %s", exc)
        # Clear the in-flight message cache for the reset session so
        # the next session's attribution does not pick up stale messages.
        sid = str(kwargs.get("session_id") or "")
        if sid:
            self._session_messages.pop(sid, None)
            self._session_request_count.pop(sid, None)

    def on_skill_lifecycle(self, **kwargs: Any) -> None:
        """Hook: ``on_skill_lifecycle`` (Hermes notify-on-skill-bump).

        Persists the fact only — never the skill's content.  ``action`` is
        a string like ``loaded`` / ``created`` / ``patched`` / ``archived`` /
        ``installed`` / ``restored`` / ``stale``.  ``use_count``, ``reused``,
        and ``reuse_after_patch`` are optional booleans / integers.
        """
        try:
            self.db.insert_skill_event(
                session_id=str(kwargs.get("session_id") or "") or None,
                task_id=str(kwargs.get("task_id") or "") or None,
                turn_id=str(kwargs.get("turn_id") or "") or None,
                skill_name=str(kwargs.get("skill_name") or ""),
                action=str(kwargs.get("action") or "unknown"),
                use_count=kwargs.get("use_count"),
                reused=kwargs.get("reused"),
                reuse_after_patch=kwargs.get("reuse_after_patch"),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("on_skill_lifecycle failed: %s", exc)

    def subagent_start(self, **kwargs: Any) -> None:
        """Hook: a subagent is about to be spawned.

        Observer-only — never vetoes the spawn (use pre_tool_call for that).
        Records the lifecycle fact so the dashboard can show subagent
        activity per session.
        """
        try:
            self.db.insert_event(
                session_id=str(kwargs.get("session_id") or "") or None,
                event_type="subagent_start",
                payload=sanitize_dict(kwargs),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("subagent_start failed: %s", exc)

    def subagent_stop(self, **kwargs: Any) -> None:
        """Hook: a subagent has stopped.

        Records lifecycle fact; payload may include a child_subagent_id
        and parent_session_id but the dashboard only ever shows counts.
        """
        try:
            self.db.insert_event(
                session_id=str(kwargs.get("session_id") or "") or None,
                event_type="subagent_stop",
                payload=sanitize_dict(kwargs),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("subagent_stop failed: %s", exc)

    # ------------------------------------------------------------------
    # Per-turn observer hooks (pre_llm_call / post_llm_call)
    # ------------------------------------------------------------------

    def pre_llm_call(self, **kwargs: Any) -> None:
        """Hook: a turn is about to run. PASSIVE observer.

        Hermes's contract is that ``pre_llm_call`` callbacks may return a
        string or ``{"context": str}`` to inject extra context.  We never
        inject — returning ``None`` keeps us strictly observational.
        """
        try:
            session_id = str(kwargs.get("session_id") or "") or None
            self.db.insert_event(
                session_id=session_id,
                event_type="pre_llm_call",
                payload={
                    "model": kwargs.get("model"),
                    "provider": kwargs.get("provider"),
                    "api_mode": kwargs.get("api_mode"),
                    "messages_count": kwargs.get("messages_count"),
                },
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("pre_llm_call failed: %s", exc)
        return None

    def post_llm_call(self, **kwargs: Any) -> None:
        """Hook: a turn finished. PASSIVE observer; always returns None."""
        try:
            session_id = str(kwargs.get("session_id") or "") or None
            self.db.insert_event(
                session_id=session_id,
                event_type="post_llm_call",
                payload={
                    "model": kwargs.get("model"),
                    "provider": kwargs.get("provider"),
                    "usage": kwargs.get("usage"),
                    "finish_reason": kwargs.get("finish_reason"),
                    "duration_s": kwargs.get("api_duration"),
                },
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("post_llm_call failed: %s", exc)
        return None

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
            messages=list(messages) if messages else None,
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

        # Hermes may cap the visible hook payload at HERMES_PLUGIN_PAYLOAD_MAX_CHARS
        # (default 50 000).  When that fires, the response object is
        # replaced with the sentinel ``{"_truncated": True, "preview": ...}``
        # (see Hermes's _sanitize_hook_payload).  We MUST detect this so
        # we do not attribute the truncated portion as "the full prompt".
        payload_truncated = _detect_payload_truncation(kwargs.get("response"))
        prompt_visible_chars = None
        prompt_visible_confidence = 0.0
        if pending and pending.messages:
            # Tokenise the cached messages (the ones Hermes actually
            # shipped to us). When truncated, drop the attribution entirely
            # and surface the gap in the report.
            total_chars = 0
            for m in pending.messages:
                if not isinstance(m, Mapping):
                    continue
                total_chars += len(_content_to_text(m.get("content")))
            prompt_visible_chars = total_chars
            tokenizer = get_tokenizer(model)
            prompt_visible_tokens_est = tokenizer.count("x" * total_chars).tokens
            if payload_truncated:
                prompt_visible_confidence = 0.4
            else:
                prompt_visible_confidence = 1.0
        prompt_visible_provenance = (
            "PROVIDER_MEASURED" if not payload_truncated
            else "HERMES_MEASURED"   # Hermes itself told us it's truncated
        )

        prompt_prov = usage.prompt_tokens.provenance if usage.prompt_tokens.value is not None else PROVENANCE_UNAVAILABLE
        input_prov = usage.input_tokens.provenance
        output_prov = usage.output_tokens.provenance
        reasoning_prov = usage.reasoning_tokens.provenance
        cache_read_prov = usage.cache_read_tokens.provenance
        cache_write_prov = usage.cache_write_tokens.provenance
        total_prov = usage.total_tokens.provenance

        # Token-weighted session cache hit prerequisites: carry
        # weight_cached and weight_prompt on the request row so the
        # session aggregate is one SUM() away.
        weight_cached = usage.cache_read_tokens.value
        weight_prompt = usage.prompt_tokens.value

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
                    "prompt_visible_chars": prompt_visible_chars,
                    "prompt_visible_provenance": prompt_visible_provenance,
                    "prompt_visible_tokens_est": prompt_visible_tokens_est if pending and pending.messages else None,
                    "prompt_visible_confidence": prompt_visible_confidence,
                    "payload_truncated": 1 if payload_truncated else 0,
                    "weight_cached": weight_cached,
                    "weight_prompt": weight_prompt,
                    "metadata_json": json.dumps(metadata),
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("post_api_request persist failed: %s", exc)
            return

        # Local prompt-component attribution.  If Hermes truncated the
        # visible payload we skip attribution entirely — the breakdown
        # we have is not the breakdown the provider paid for.
        if not payload_truncated:
            try:
                messages = self._session_messages.get(session_id) or []
                if messages:
                    components = attribute_messages(
                        messages, get_tokenizer(model)
                    )
                    self.db.insert_prompt_components(
                        row_id, [c.as_dict() for c in components]
                    )
            except Exception as exc:  # pragma: no cover
                logger.warning("attribution failed: %s", exc)

        # Auto-analyze (cheap rule pass) if enabled.
        if self.config.run_analyzer:
            try:
                self._run_analyzer_for_request(row_id, session_id, usage, cache_hit)
            except Exception as exc:  # pragma: no cover
                logger.warning("analyzer failed: %s", exc)

        # Issue 12: backfill api_request_row_id on any tool call that
        # Hermes attributes to this API request.  Hermes does not give us
        # a tool_call_id -> api_request_id link directly; we infer it by
        # matching on (turn_id, api_call_count) for in-flight tool calls
        # recorded in the same session.
        self._backfill_tool_request_links(session_id, row_id, kwargs)

        # Local context-delta attribution between this request and the
        # previous one in the same session.  Issue 12.
        self._maybe_record_context_delta(
            session_id=session_id,
            current_row_id=row_id,
            current_prompt=usage.prompt_tokens.value,
            payload_truncated=payload_truncated,
        )

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
        # Issue 11: NEVER persist the raw args summary. We only keep the
        # arg hash and a key list (no values). The full command line is
        # recoverable only via the hash, which is fine for de-dup
        # detection but not for reconstruction.
        args_summary = None
        pending = _PendingToolCall(
            session_id=session_id,
            turn_id=kwargs.get("turn_id"),
            api_request_row_id=None,  # we don't have a stable link here
            tool_call_id=tool_call_id,
            tool_name=str(kwargs.get("tool_name") or ""),
            args_summary=args_summary,
            started_at=float(kwargs.get("started_at") or time.time()),
            args_hash=args_hash,
            args=dict(args) if isinstance(args, dict) else None,
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
        output_tokenizer = get_tokenizer()
        output_tokens_est = output_tokenizer.count("x" * output_chars).tokens if output_chars else 0
        output_hash = hash_text(result_str)
        output_truncated = _looks_truncated(result_str)

        tool_name = kwargs.get("tool_name") or (pending.tool_name if pending else "")
        # Issue 9 + Issue 11: prefer the args from the pre hook so the
        # post hook can still build command family / path summary even
        # when Hermes only sends the result.
        raw_args = (
            (pending.args if pending and isinstance(pending.args, dict) else None)
            or (kwargs.get("args") or {})
        )
        args_keys = sorted(k for k in raw_args.keys() if isinstance(k, str)) if isinstance(raw_args, Mapping) else []
        args_chars = len(_safe_json(raw_args)) if raw_args else 0
        input_tokens_est = output_tokenizer.count("x" * args_chars).tokens if args_chars else 0
        args_summary = pending.args_summary if pending else None
        command = _extract_command(raw_args) if isinstance(raw_args, Mapping) else None
        command_family = _command_family(command) if command else ""
        command_hash = _command_hash(command) if command else ""
        path_ext, path_hash, path_basename, file_path_stored = (
            _summarize_path(raw_args) if isinstance(raw_args, Mapping) else ("", "", "", 0)
        )

        category = classify_tool(str(tool_name), raw_args)

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
                    # Issue 11: sanitise error messages
                    "error_message": _safe_json(
                        sanitize_dict({"error_message": kwargs.get("error_message")})
                    ).strip('"{}').strip() if kwargs.get("error_message") else None,
                    "exit_code": kwargs.get("exit_code"),
                    "output_truncated": output_truncated,
                    "input_chars": args_chars or None,
                    "input_tokens_est": input_tokens_est or None,
                    "output_chars": output_chars,
                    "output_tokens_est": output_tokens_est,
                    "output_hash": output_hash,
                    "args_hash": pending.args_hash if pending else hash_text(_safe_json(raw_args)),
                    "args_summary": args_summary,
                    "command_family": command_family or None,
                    "command_hash": command_hash or None,
                    "input_measurement_method": output_tokenizer.count("hello").method if args_chars else None,
                    "output_measurement_method": output_tokenizer.count("hello").method if output_chars else None,
                    "input_tokens": input_tokens_est or None,
                    "args_keys_json": json.dumps(args_keys) if args_keys else None,
                    "path_ext": path_ext or None,
                    "path_hash": path_hash or None,
                    "path_basename": path_basename or None,
                    "file_path_stored": file_path_stored,
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

    def _maybe_record_context_delta(
        self,
        *,
        session_id: str,
        current_row_id: int,
        current_prompt: Optional[int],
        payload_truncated: bool,
    ) -> None:
        """Issue 12 — log a context_deltas row between consecutive api_requests.

        We pair the current request with the previous one in the same
        session, compute the provider delta, and attribute the
        explanation locally by summing the per-component tokens.  This
        is the primary input to the dashboard's "what grew the prompt"
        view.

        We skip the row when the current payload was truncated, because
        we cannot trust the per-component breakdown.
        """
        if payload_truncated:
            return
        if current_prompt is None or current_prompt <= 0:
            return
        prev = self._last_api_request_row(session_id, exclude=current_row_id)
        if prev is None:
            self._remember_last_request(session_id, current_row_id)
            return
        previous_id, previous_prompt = prev
        if previous_prompt is None or previous_prompt <= 0:
            self._remember_last_request(session_id, current_row_id)
            return
        provider_delta = int(current_prompt) - int(previous_prompt)
        # Per-component breakdown: current minus previous.  Each row
        # contributes its local-attributed token count.  When the
        # provider delta is large, the gap between explained and
        # provider_delta is exactly the unknown chunk (cache effects,
        # server-side prefix, etc.).
        current_components = {
            c["component"]: int(c["estimated_tokens"] or 0)
            for c in self.db.prompt_components_for_request(current_row_id)
        }
        previous_components = {
            c["component"]: int(c["estimated_tokens"] or 0)
            for c in self.db.prompt_components_for_request(previous_id)
        }
        contributors: list[dict[str, Any]] = []
        explained = 0
        for comp, cur in current_components.items():
            prev = previous_components.get(comp, 0)
            delta = cur - prev
            if delta == 0:
                continue
            explained += delta
            contributors.append({"component": comp, "tokens": delta})
        contributors.sort(key=lambda d: -abs(d["tokens"]))
        unexplained = provider_delta - explained
        coverage: Optional[float] = None
        if provider_delta > 0:
            coverage = max(0.0, min(1.0, explained / provider_delta))
        try:
            self.db.insert_context_delta({
                "session_id": session_id,
                "previous_api_request_id": previous_id,
                "current_api_request_id": current_row_id,
                "provider_delta_tokens": provider_delta,
                "explained_tokens": explained,
                "unexplained_tokens": unexplained,
                "coverage": coverage,
                "contributors": contributors[:20],
                "confidence": coverage if coverage is not None else None,
            })
        except Exception as exc:  # pragma: no cover
            logger.warning("context_delta insert failed: %s", exc)
        self._remember_last_request(session_id, current_row_id)

    # ------------------------------------------------------------------
    # Tiny in-memory cache of the last (api_request_row_id, prompt_tokens)
    # per session — used by the context-delta attribution.
    # ------------------------------------------------------------------

    def _last_api_request_row(
        self, session_id: str, *, exclude: int
    ) -> Optional[tuple[int, Optional[int]]]:
        cached = self._last_request_cache.get(session_id)
        if not cached:
            return None
        row_id, prompt = cached
        if row_id == exclude:
            return None
        return row_id, prompt

    def _remember_last_request(
        self, session_id: str, row_id: int, prompt: Optional[int] = None,
    ) -> None:
        if prompt is None:
            try:
                row = self.db.api_requests_for_session(session_id)
                for r in reversed(row):
                    if r["id"] == row_id:
                        prompt = r["prompt_tokens"]
                        break
            except Exception:
                prompt = None
        self._last_request_cache[session_id] = (row_id, prompt)

    def _backfill_tool_request_links(
        self, session_id: str, api_request_row_id: int, kwargs: Mapping[str, Any]
    ) -> None:
        """Mark any tool calls in this session that match this API call.

        Hermes does not pass a tool_call_id → api_request_id pointer in
        pre/post_tool_call.  We approximate the link by turn id (and
        the per-turn api_call_count ordering).  This is best-effort;
        missing links stay NULL.
        """
        turn_id = kwargs.get("turn_id")
        if not turn_id:
            return
        try:
            tool_rows = self.db.tool_calls_for_session(session_id)
        except Exception:
            return
        for tr in tool_rows:
            if tr["api_request_row_id"] is not None:
                continue
            if tr["turn_id"] and tr["turn_id"] == turn_id:
                try:
                    self.db.update_tool_call_api_request(
                        tool_call_row_id=tr["id"],
                        api_request_row_id=api_request_row_id,
                    )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Pure helpers (tested directly in tests/test_accounting.py etc.)
# ---------------------------------------------------------------------------


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


def classify_tool(
    tool_name: str,
    args: Optional[Mapping[str, Any]] = None,
) -> str:
    """Bucket a tool name into one of our coarse categories.

    Pass ``args`` to get command-aware classification for terminal
    actions (Issue 9). When ``args`` is provided and ``tool_name`` is
    one of the terminal-shaped tools, we inspect the command string
    and upgrade the bucket to TEST / BUILD / GIT / LINT / SEARCH /
    PACKAGE as appropriate.
    """
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
    base_category = "other"
    for category, needles in mapping:
        for n in needles:
            if name == n or name.startswith(n + "_") or n in name:
                base_category = category
                break
        if base_category != "other":
            break

    # Command-aware sub-classification for terminal-shaped tools.
    if base_category == "terminal" and args:
        command = _extract_command(args)
        if command:
            sub = classify_command(command)
            if sub != "other":
                return sub
    return base_category


# Command-aware classification (Issue 9). All matches are
# case-insensitive and substring-based; we deliberately use generous
# prefixes ("npm test", "pnpm test", "yarn test") so pnpm / yarn / bun
# variants land in the right bucket.
_COMMAND_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("git", (
        "git ", "git\t", "git\n", "/git",  # space/tab/newline or path
    )),
    ("test", (
        "pytest", "py.test", "unittest", "python -m pytest", "python -m unittest",
        "npm test", "pnpm test", "yarn test", "bun test", "vitest", "jest ",
        "mocha ", "tox ", "nox ", "rspec", "phpunit", "go test", "cargo test",
    )),
    ("lint", (
        "ruff ", "ruff check", "ruff format",
        "pylint ", "flake8 ", "mypy ", "pyright ",
        "eslint ", "tslint ", "biome ", "prettier --",
        "rubocop", "golangci-lint", "shellcheck",
    )),
    ("build", (
        "npm run build", "pnpm run build", "yarn build", "bun run build",
        "tsc ", "tsc --", "webpack ", "vite build", "rollup -c",
        "make ", "make\t", "make\n", "/make",
        "npm run compile", "pnpm run compile",
        "cargo build", "go build", "mvn package", "gradle build",
        "docker build", "docker-compose build",
    )),
    ("package", (
        "pip install", "pip uninstall", "npm install", "pnpm install",
        "yarn add", "yarn install", "bun add", "bun install",
        "cargo add", "go get", "go mod",
        "docker pull", "docker push", "docker run", "docker exec",
    )),
    ("search", (
        "rg ", "rg\t", "rg\n", "ripgrep",  # explicit
        "grep ", "grep\t", "grep\n", "grep$",
        "ag ", "ag\t", "ag\n",  # silver searcher
        "fd ", "find ", "locate ",
        "grep -", "rg -",
    )),
    ("terminal", ()),  # catch-all so the tuple can be checked
)


def classify_command(command: str) -> str:
    """Classify a terminal command into TEST / BUILD / GIT / LINT / etc.

    Returns "terminal" when no specific sub-classification applies. The
    function is deliberately conservative: when in doubt it returns
    "terminal" rather than guessing.
    """
    if not command:
        return "terminal"
    s = command.strip().lower()
    if not s:
        return "terminal"
    # Strip a leading `sudo` or `env` so `sudo git diff` still classifies.
    for prefix in ("sudo ", "doas ", "env "):
        if s.startswith(prefix):
            s = s[len(prefix):].lstrip()
    for category, patterns in _COMMAND_PATTERNS:
        for pat in patterns:
            if s.startswith(pat) or pat in s:
                return category
    return "terminal"


def _extract_command(args: Mapping[str, Any]) -> Optional[str]:
    """Best-effort pull the actual command line out of a tool-call ``args`` dict."""
    if not isinstance(args, Mapping):
        return None
    for key in ("command", "cmd", "shell_command", "bash_command"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    # Some tools use {"argv": [...]} or {"args": "..."} or a nested shape.
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return " ".join(str(x) for x in argv)
    nested = args.get("args")
    if isinstance(nested, str) and nested.strip():
        return nested
    return None


def _command_family(command: str) -> str:
    """Stable, low-cardinality label for grouping in the dashboard."""
    cmd = (command or "").strip()
    if not cmd:
        return ""
    # First whitespace-delimited token, with path prefix stripped.
    head = cmd.split(None, 1)[0]
    head = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Common wrapper / build-tool aliases.
    aliases = {
        "pnpm": "npm", "yarn": "npm", "bun": "npm",
        "npx": "npm", "uv": "pip", "pip3": "pip", "pipx": "pip",
    }
    return aliases.get(head, head)


def _command_hash(command: str) -> str:
    """SHA256 of the canonicalised command line."""
    return hash_text((command or "").strip())


# A curated list of keys that frequently carry an absolute file path in
# tool-call arg dicts. Issue 11: we record only the extension / hash /
# basename of the path, never the full path.
_PATH_KEYS: tuple[str, ...] = (
    "path", "file_path", "file", "filepath", "filename", "absolute_path",
    "target_path", "source_path", "dest_path", "input_path", "output_path",
    "notebook_path", "repo_path", "cwd", "directory",
)


def _summarize_path(args: Mapping[str, Any]) -> tuple[str, str, str, int]:
    """Best-effort extraction of a file-path reference from tool-call args.

    Returns ``(extension, path_hash, basename, file_path_stored)`` where
    ``file_path_stored`` is 1 if a plausible path-like string was found,
    0 otherwise.  We never persist the raw path string.
    """
    if not isinstance(args, Mapping):
        return ("", "", "", 0)
    for key in _PATH_KEYS:
        v = args.get(key)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        # Strip surrounding quotes a shell may have left in.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        basename = v.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not basename or basename == ".":
            continue
        if "." in basename:
            ext = "." + basename.rsplit(".", 1)[-1].lower()
            if len(ext) > 16:
                ext = ""
        else:
            ext = ""
        return (ext, hash_text(v), basename, 1)
    return ("", "", "", 0)


def _detect_payload_truncation(response: Any) -> bool:
    """Return True when the response payload was replaced by Hermes's
    ``HERMES_PLUGIN_PAYLOAD_MAX_CHARS`` truncation sentinel.

    The sentinel shape is documented in Hermes's run_agent.py:
    ``{"_truncated": True, "original_type": "...", "preview": "..."}``.
    """
    if not isinstance(response, Mapping):
        return False
    return bool(response.get("_truncated"))


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