"""Rule-based analyzer.

The rules are deliberately simple and the wording is cautious — we
NEVER call something ``waste`` definitively.  Findings use the
vocabulary the spec asks for: ``POTENTIAL_WASTE``, ``HIGH_OVERHEAD``,
``REPEATED_CONTENT``, ``OBSERVATION``.

The analyzer is invoked once per recorded ``post_api_request`` and is
deliberately O(1) per call (it only inspects the most recent few
records for the same session).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_checker.accounting import UsageSummary
from hermes_checker.storage import Database

from ..collector.config import CollectorConfig

logger = logging.getLogger("hermes_checker.analysis")

POTENTIAL_WASTE = "POTENTIAL_WASTE"
HIGH_OVERHEAD = "HIGH_OVERHEAD"
REPEATED_CONTENT = "REPEATED_CONTENT"
OBSERVATION = "OBSERVATION"


@dataclass
class Finding:
    finding_kind: str
    severity: str
    confidence: float
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


class Analyzer:
    """Stateless rule-based analyzer."""

    def __init__(self, db: Database, config: CollectorConfig) -> None:
        self.db = db
        self.config = config

    def analyze_request(
        self,
        *,
        api_request_row_id: int,
        session_id: str,
        usage: UsageSummary,
        cache_hit_ratio: Optional[float],
    ) -> list[Finding]:
        out: list[Finding] = []

        # Rule: large context jump vs the previous request in the same session.
        prev = self._previous_request(api_request_row_id, session_id)
        if prev is not None:
            prev_prompt = prev["prompt_tokens"] or 0
            cur_prompt = usage.prompt_tokens.value or 0
            delta = cur_prompt - prev_prompt
            if delta >= self.config.large_jump_tokens:
                confidence = min(1.0, delta / max(self.config.large_jump_tokens * 2, 1))
                out.append(Finding(
                    finding_kind="large_context_jump",
                    severity=POTENTIAL_WASTE,
                    confidence=confidence,
                    message=(
                        f"Prompt size grew by ~{delta:,} tokens "
                        f"between consecutive API calls."
                    ),
                    evidence={
                        "previous_prompt_tokens": prev_prompt,
                        "current_prompt_tokens": cur_prompt,
                        "delta_tokens": delta,
                        "previous_api_request_id": prev["api_request_id"],
                    },
                ))

        # Rule: poor cache period — current hit ratio noticeably below
        # the session's recent average.
        if cache_hit_ratio is not None:
            recent_avg = self._recent_cache_hit_ratio(session_id, limit=10)
            if recent_avg is not None and cache_hit_ratio < recent_avg - 0.2 and cache_hit_ratio < self.config.cache_hit_floor:
                confidence = min(1.0, (recent_avg - cache_hit_ratio) * 2)
                out.append(Finding(
                    finding_kind="cache_miss_burst",
                    severity=HIGH_OVERHEAD,
                    confidence=confidence,
                    message=(
                        f"Cache hit dropped to {cache_hit_ratio:.0%} "
                        f"(recent session average was {recent_avg:.0%})."
                    ),
                    evidence={
                        "current_cache_hit": cache_hit_ratio,
                        "session_average_cache_hit": recent_avg,
                    },
                ))

        # Rule: very large uncached prompt relative to a fixed-overhead baseline.
        if usage.prompt_tokens.value is not None and usage.cache_read_tokens.value is not None:
            uncached = max(0, usage.prompt_tokens.value - usage.cache_read_tokens.value)
            if uncached >= 50_000:
                confidence = min(1.0, uncached / 100_000)
                out.append(Finding(
                    finding_kind="large_uncached_prompt",
                    severity=HIGH_OVERHEAD,
                    confidence=confidence,
                    message=(
                        f"~{uncached:,} fresh tokens were sent in this request "
                        f"(cache hit was "
                        f"{usage.cache_read_tokens.value / max(usage.prompt_tokens.value, 1):.0%})."
                    ),
                    evidence={
                        "uncached_tokens": uncached,
                        "cached_tokens": usage.cache_read_tokens.value,
                        "prompt_tokens": usage.prompt_tokens.value,
                    },
                ))

        # Persist findings
        for f in out:
            try:
                self.db.insert_finding(
                    session_id=session_id,
                    finding_kind=f.finding_kind,
                    severity=f.severity,
                    confidence=f.confidence,
                    detected_at=time.time(),
                    evidence=f.evidence,
                    message=f.message,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("insert_finding failed: %s", exc)

        return out

    def analyze_session(self, session_id: str) -> list[Finding]:
        """Run a fuller sweep over a session (used by ``report`` / dashboard)."""
        out: list[Finding] = []
        rows = self.db.api_requests_for_session(session_id)
        if not rows:
            return out

        tool_rows = self.db.tool_calls_for_session(session_id)
        tool_counts: dict[str, int] = {}
        for tr in tool_rows:
            tool_counts[tr["category"] or "other"] = tool_counts.get(tr["category"] or "other", 0) + 1

        if tool_counts:
            most_common = max(tool_counts.items(), key=lambda kv: kv[1])
            if most_common[1] >= 10:
                out.append(Finding(
                    finding_kind="tool_category_dominance",
                    severity=OBSERVATION,
                    confidence=min(1.0, most_common[1] / sum(tool_counts.values())),
                    message=(
                        f"Most-used tool category in this session is '{most_common[0]}' "
                        f"({most_common[1]} of {sum(tool_counts.values())} calls)."
                    ),
                    evidence={"counts": tool_counts},
                ))

        # Repeated identical tool outputs in the same session.
        seen: dict[str, int] = {}
        for tr in tool_rows:
            h = tr["output_hash"]
            if not h:
                continue
            seen[h] = seen.get(h, 0) + 1
        repeated = {h: n for h, n in seen.items() if n >= 3}
        if repeated:
            total = sum(repeated.values())
            out.append(Finding(
                finding_kind="repeated_tool_output",
                severity=REPEATED_CONTENT,
                confidence=min(1.0, total / max(len(tool_rows), 1)),
                message=(
                    f"{total} tool calls in this session produced output already seen at least twice."
                ),
                evidence={"repeated_count": repeated, "total_tool_calls": len(tool_rows)},
            ))

        # Large terminal outputs
        big_terminals = [
            tr for tr in tool_rows
            if tr["category"] == "terminal"
            and tr["output_chars"] is not None
            and tr["output_chars"] >= self.config.large_tool_output_chars
        ]
        if big_terminals:
            out.append(Finding(
                finding_kind="large_terminal_outputs",
                severity=POTENTIAL_WASTE,
                confidence=min(1.0, len(big_terminals) / max(len(tool_rows), 1)),
                message=(
                    f"{len(big_terminals)} terminal calls produced "
                    f"≥ {self.config.large_tool_output_chars:,} characters each."
                ),
                evidence={"count": len(big_terminals)},
            ))

        # Persist findings as session-level observations
        for f in out:
            try:
                self.db.insert_finding(
                    session_id=session_id,
                    finding_kind=f.finding_kind,
                    severity=f.severity,
                    confidence=f.confidence,
                    detected_at=time.time(),
                    evidence=f.evidence,
                    message=f.message,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("insert_finding failed: %s", exc)

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _previous_request(
        self,
        current_id: int,
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        rows = self.db.api_requests_for_session(session_id)
        prev: Optional[sqlite3_row] = None  # type: ignore[name-defined]
        for row in rows:
            if row["id"] == current_id:
                break
            prev = row
        if prev is None:
            return None
        return {
            "api_request_id": prev["api_request_id"],
            "prompt_tokens": prev["prompt_tokens"],
        }

    def _recent_cache_hit_ratio(self, session_id: str, *, limit: int) -> Optional[float]:
        rows = self.db.api_requests_for_session(session_id)
        recent = rows[-limit:]
        hits = [r["cache_hit_ratio"] for r in recent if r["cache_hit_ratio"] is not None]
        if not hits:
            return None
        return sum(hits) / len(hits)


# Late import to avoid a circular dep at module load.
import sqlite3 as _sqlite3  # noqa: E402  (used only in type alias)
sqlite3_row = _sqlite3.Row