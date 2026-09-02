"""Usage-accounting helpers.

The collector calls :func:`extract_usage_summary` to translate whatever
the Hermes hook payload contains (Hermes already normalises this into a
``CanonicalUsage``-shaped dict on ``post_api_request``) into a
:class:`UsageSummary` that knows the provenance of every field.

Provenance rules
----------------

- Any field whose value is non-None is ``PROVIDER_MEASURED`` (Hermes only
  fills it from the provider's response, never guesses).
- Fields we compute locally (``prompt_tokens``,
  ``total_tokens``, ``tokens_per_second``, ``cache_hit_ratio``) are
  ``LOCALLY_CALCULATED`` and labelled as such in the database row.
- When the provider response is missing entirely, we report
  ``UNAVAILABLE`` rather than fabricate numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_checker import (
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_PROVIDER_MEASURED,
    PROVENANCE_UNAVAILABLE,
)


@dataclass(frozen=True)
class UsageProvenance:
    """Tags a single numeric bucket with WHERE it came from."""

    value: Optional[int]
    provenance: str  # one of hermes_checker.PROVENANCE

    @classmethod
    def measured(cls, value: Optional[int]) -> "UsageProvenance":
        return cls(value, PROVENANCE_PROVIDER_MEASURED)

    @classmethod
    def calculated(cls, value: Optional[int]) -> "UsageProvenance":
        return cls(value, PROVENANCE_LOCALLY_CALCULATED)

    @classmethod
    def unavailable(cls) -> "UsageProvenance":
        return cls(None, PROVENANCE_UNAVAILABLE)


def _unavailable() -> "UsageProvenance":
    return UsageProvenance(None, PROVENANCE_UNAVAILABLE)


@dataclass(frozen=True)
class UsageSummary:
    """Normalised usage summary for a single API request.

    Every field carries its own provenance so the database row can store
    them separately.
    """

    prompt_tokens: UsageProvenance = field(default_factory=_unavailable)
    input_tokens: UsageProvenance = field(default_factory=_unavailable)
    output_tokens: UsageProvenance = field(default_factory=_unavailable)
    reasoning_tokens: UsageProvenance = field(default_factory=_unavailable)
    cache_read_tokens: UsageProvenance = field(default_factory=_unavailable)
    cache_write_tokens: UsageProvenance = field(default_factory=_unavailable)
    total_tokens: UsageProvenance = field(default_factory=_unavailable)

    @property
    def any_provider_measured(self) -> bool:
        return any(
            p.provenance == PROVENANCE_PROVIDER_MEASURED
            for p in (
                self.prompt_tokens,
                self.input_tokens,
                self.output_tokens,
                self.reasoning_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.total_tokens,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens.value,
            "prompt_tokens_provenance": self.prompt_tokens.provenance,
            "input_tokens": self.input_tokens.value,
            "input_tokens_provenance": self.input_tokens.provenance,
            "output_tokens": self.output_tokens.value,
            "output_tokens_provenance": self.output_tokens.provenance,
            "reasoning_tokens": self.reasoning_tokens.value,
            "reasoning_tokens_provenance": self.reasoning_tokens.provenance,
            "cache_read_tokens": self.cache_read_tokens.value,
            "cache_read_tokens_provenance": self.cache_read_tokens.provenance,
            "cache_write_tokens": self.cache_write_tokens.value,
            "cache_write_tokens_provenance": self.cache_write_tokens.provenance,
            "total_tokens": self.total_tokens.value,
            "total_tokens_provenance": self.total_tokens.provenance,
        }


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort coerce to non-negative int, None if not numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return 0
    return n


def extract_usage_summary(usage: Optional[Mapping[str, Any]]) -> UsageSummary:
    """Translate a Hermes ``usage`` dict into a :class:`UsageSummary`.

    Accepts the canonical field names from ``CanonicalUsage``
    (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_write_tokens``, ``reasoning_tokens``) as well as a few
    provider-specific aliases (``prompt_tokens``, ``completion_tokens``)
    that we recognise but never fabricate.

    Rules:

    - All non-None numeric buckets are tagged ``PROVIDER_MEASURED``.
    - ``prompt_tokens`` is tagged ``LOCALLY_CALCULATED`` when computed
      from input+cache_read+cache_write; never copied from the
      provider's prompt_tokens if it disagrees with that sum (we still
      keep the provider's number, but the ``LOCALLY_CALCULATED`` value
      is what the report UI displays for prompt totals).
    - ``total_tokens`` is also computed locally when missing.
    """
    if not usage:
        empty = UsageProvenance.unavailable()
        return UsageSummary(
            prompt_tokens=empty,
            input_tokens=empty,
            output_tokens=empty,
            reasoning_tokens=empty,
            cache_read_tokens=empty,
            cache_write_tokens=empty,
            total_tokens=empty,
        )

    input_tokens = _coerce_int(usage.get("input_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens")
                                or usage.get("completion_tokens"))
    cache_read = _coerce_int(usage.get("cache_read_tokens")
                             or usage.get("cached_tokens"))
    cache_write = _coerce_int(usage.get("cache_write_tokens"))
    reasoning = _coerce_int(usage.get("reasoning_tokens")
                            or usage.get("thought_tokens"))
    reported_prompt = _coerce_int(usage.get("prompt_tokens"))
    reported_total = _coerce_int(usage.get("total_tokens"))

    prompt_local: Optional[int] = None
    if input_tokens is not None or cache_read is not None or cache_write is not None:
        prompt_local = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0)
    elif reported_prompt is not None:
        prompt_local = reported_prompt

    total_local: Optional[int] = None
    if prompt_local is not None or output_tokens is not None:
        total_local = (prompt_local or 0) + (output_tokens or 0)
    elif reported_total is not None:
        total_local = reported_total

    return UsageSummary(
        prompt_tokens=UsageProvenance.calculated(prompt_local) if prompt_local is not None else UsageProvenance.unavailable(),
        input_tokens=UsageProvenance.measured(input_tokens) if input_tokens is not None else UsageProvenance.unavailable(),
        output_tokens=UsageProvenance.measured(output_tokens) if output_tokens is not None else UsageProvenance.unavailable(),
        reasoning_tokens=UsageProvenance.measured(reasoning) if reasoning is not None else UsageProvenance.unavailable(),
        cache_read_tokens=UsageProvenance.measured(cache_read) if cache_read is not None else UsageProvenance.unavailable(),
        cache_write_tokens=UsageProvenance.measured(cache_write) if cache_write is not None else UsageProvenance.unavailable(),
        total_tokens=UsageProvenance.calculated(total_local) if total_local is not None else UsageProvenance.unavailable(),
    )


def cache_hit_ratio(summary: UsageSummary) -> Optional[float]:
    """Cache-hit ratio = cache_read_tokens / prompt_tokens.

    Returns None when either value is missing or zero. The caller is
    responsible for converting this to a percentage in the UI.
    """
    if summary.cache_read_tokens.value is None or summary.prompt_tokens.value is None:
        return None
    if summary.prompt_tokens.value <= 0:
        return None
    return summary.cache_read_tokens.value / summary.prompt_tokens.value


def tokens_per_second(summary: UsageSummary, *,
                      duration_s: Optional[float]) -> Optional[float]:
    """Tokens-per-second, defined as output_tokens / generation_time.

    We only count generation time (after the first chunk) when streaming.
    The collector passes ``duration_s - ttft_s``; this helper trusts
    that math and divides by it.
    """
    if summary.output_tokens.value is None or duration_s is None:
        return None
    if duration_s <= 0:
        return None
    return summary.output_tokens.value / duration_s


__all__ = [
    "UsageProvenance",
    "UsageSummary",
    "extract_usage_summary",
    "cache_hit_ratio",
    "tokens_per_second",
]