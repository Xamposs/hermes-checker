"""Report assembly.

The ``build_report`` function aggregates everything Hermes Checker has
recorded for a session into a single, printable structure used by both
the CLI (``hermes-checker report``) and the dashboard's SESSION view.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from hermes_checker.accounting import (
    PricingEntry,
    PricingTable,
    compute_cost,
)
from hermes_checker.storage import Database


@dataclass
class SessionTotals:
    """Provider-measured totals for a session."""

    api_requests: int
    tool_calls: int
    prompt_tokens: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    duration_s_total: float
    duration_s_api_avg: Optional[float]
    ttft_s_avg: Optional[float]
    tps_avg: Optional[float]
    cache_hit_ratio_avg: Optional[float]
    provider_cost_usd: Optional[float]
    projected_cost_usd: Optional[float]
    projected_cost_profile: Optional[str]
    streaming_requests: int


@dataclass
class ComponentBreakdown:
    component: str
    estimated_tokens: int
    percentage: float
    measurement_method: str
    confidence: float


@dataclass
class ToolCategoryBreakdown:
    category: str
    calls: int
    estimated_output_tokens: int
    percentage_of_calls: float


@dataclass
class SessionReport:
    session_id: str
    profile: Optional[str]
    platform: Optional[str]
    started_at: Optional[float]
    ended_at: Optional[float]
    experiment: Optional[str]
    totals: SessionTotals
    component_breakdown: list[ComponentBreakdown]
    tool_breakdown: list[ToolCategoryBreakdown]
    findings: list[dict[str, Any]]
    attribution_error_tokens: Optional[int]
    provenance_notes: dict[str, str]
    latest_request: Optional[dict[str, Any]] = None


def build_report(
    db: Database,
    session_id: str,
    *,
    pricing: Optional[Mapping[str, PricingTable]] = None,
    profile_name: Optional[str] = None,
) -> SessionReport:
    """Assemble a :class:`SessionReport` for the given session."""
    session = db.session(session_id)
    api_rows = db.api_requests_for_session(session_id)
    tool_rows = db.tool_calls_for_session(session_id)
    findings = [dict(r) for r in db.findings(session_id=session_id)]

    # Aggregate provider-measured totals (skip None / 0 appropriately).
    prompt_tokens = sum((r["prompt_tokens"] or 0) for r in api_rows)
    input_tokens = sum((r["input_tokens"] or 0) for r in api_rows)
    output_tokens = sum((r["output_tokens"] or 0) for r in api_rows)
    reasoning_tokens = sum((r["reasoning_tokens"] or 0) for r in api_rows)
    cache_read = sum((r["cache_read_tokens"] or 0) for r in api_rows)
    cache_write = sum((r["cache_write_tokens"] or 0) for r in api_rows)
    total_tokens = sum((r["total_tokens"] or 0) for r in api_rows)

    durations = [r["duration_s"] for r in api_rows if r["duration_s"] is not None]
    ttfts = [r["ttft_s"] for r in api_rows if r["ttft_s"] is not None]
    tps = [r["tokens_per_second"] for r in api_rows if r["tokens_per_second"] is not None]
    cache_ratios = [r["cache_hit_ratio"] for r in api_rows if r["cache_hit_ratio"] is not None]

    provider_cost: Optional[float] = None  # We don't have a provider_cost column in V1.
    projected_cost: Optional[float] = None
    projected_cost_profile: Optional[str] = None

    duration_api_avg = statistics.mean(durations) if durations else None
    ttft_avg = statistics.mean(ttfts) if ttfts else None
    tps_avg = statistics.mean(tps) if tps else None
    cache_hit_avg = statistics.mean(cache_ratios) if cache_ratios else None

    streaming_requests = sum(1 for r in api_rows if r["streaming"])

    totals = SessionTotals(
        api_requests=len(api_rows),
        tool_calls=len(tool_rows),
        prompt_tokens=prompt_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=total_tokens,
        duration_s_total=sum(durations),
        duration_s_api_avg=duration_api_avg,
        ttft_s_avg=ttft_avg,
        tps_avg=tps_avg,
        cache_hit_ratio_avg=cache_hit_avg,
        provider_cost_usd=provider_cost,
        projected_cost_usd=projected_cost,
        projected_cost_profile=projected_cost_profile,
        streaming_requests=streaming_requests,
    )

    # Component breakdown — roll up per request
    component_rows: dict[str, dict[str, Any]] = {}
    for r in api_rows:
        for comp in db.prompt_components_for_request(r["id"]):
            c = component_rows.setdefault(
                comp["component"],
                {
                    "tokens": 0,
                    "method": comp["measurement_method"],
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                },
            )
            c["tokens"] += comp["estimated_tokens"] or 0
            c["confidence_sum"] += comp["confidence"] or 0
            c["confidence_count"] += 1

    component_total = sum(c["tokens"] for c in component_rows.values()) or 1
    component_breakdown = [
        ComponentBreakdown(
            component=name,
            estimated_tokens=int(c["tokens"]),
            percentage=c["tokens"] / component_total * 100,
            measurement_method=str(c["method"]),
            confidence=c["confidence_sum"] / c["confidence_count"] if c["confidence_count"] else 0.5,
        )
        for name, c in sorted(
            component_rows.items(),
            key=lambda kv: kv[1]["tokens"],
            reverse=True,
        )
    ]

    # Tool breakdown
    tool_total = max(len(tool_rows), 1)
    cat_counts: dict[str, dict[str, int]] = {}
    for tr in tool_rows:
        c = cat_counts.setdefault(tr["category"] or "other", {"calls": 0, "tokens": 0})
        c["calls"] += 1
        c["tokens"] += tr["output_tokens_est"] or 0
    tool_breakdown = [
        ToolCategoryBreakdown(
            category=name,
            calls=v["calls"],
            estimated_output_tokens=v["tokens"],
            percentage_of_calls=v["calls"] / tool_total * 100,
        )
        for name, v in sorted(cat_counts.items(), key=lambda kv: kv[1]["calls"], reverse=True)
    ]

    attribution_error: Optional[int] = None
    if prompt_tokens:
        attribution_error = int(component_total) - prompt_tokens

    provenance_notes = {
        "prompt_tokens": "PROVIDER_MEASURED (Hermes canonicalises the provider's usage dict; if missing, left NULL with provenance=UNAVAILABLE).",
        "input_tokens": "PROVIDER_MEASURED.",
        "output_tokens": "PROVIDER_MEASURED.",
        "reasoning_tokens": "PROVIDER_MEASURED when the provider reports them separately; otherwise UNKNOWN.",
        "cache_read_tokens": "PROVIDER_MEASURED when the provider reports cache reads; many free providers do not, so this often is 0.",
        "cache_write_tokens": "PROVIDER_MEASURED when the provider reports cache writes.",
        "total_tokens": "LOCALLY_CALCULATED as prompt + output.",
        "component_breakdown": "LOCALLY_ESTIMATED (tokenised from Hermes's last outgoing messages payload; provider does not break down by section).",
        "tool_breakdown": "LOCALLY_ESTIMATED (characters/4 fallback unless tiktoken is installed).",
    }

    # Projected cost — apply the requested pricing profile if given.
    if pricing is not None and profile_name:
        table = pricing.get(profile_name) or pricing.get("default")
        if table is not None:
            entry = _pick_entry(table, api_rows)
            projected_cost = _cost_against_entry(entry, api_rows)
            projected_cost_profile = profile_name

    latest = None
    if api_rows:
        last = api_rows[-1]
        latest = _api_row_to_dict(last)

    return SessionReport(
        session_id=session_id,
        profile=(session["profile"] if session else None),
        platform=(session["platform"] if session else None),
        started_at=(session["started_at"] if session else None),
        ended_at=(session["ended_at"] if session else None),
        experiment=(session["experiment"] if session else None),
        totals=totals,
        component_breakdown=component_breakdown,
        tool_breakdown=tool_breakdown,
        findings=findings,
        attribution_error_tokens=attribution_error,
        provenance_notes=provenance_notes,
        latest_request=latest,
    )


def render_text(report: SessionReport, *, pricing_profile: Optional[str] = None) -> str:
    """Render the report as a console-friendly text block."""
    lines: list[str] = []
    lines.append("HERMES CHECKER — SESSION REPORT")
    lines.append("=" * 60)
    lines.append(f"Session:        {report.session_id}")
    if report.profile:
        lines.append(f"Profile:        {report.profile}")
    if report.platform:
        lines.append(f"Platform:       {report.platform}")
    if report.experiment:
        lines.append(f"Experiment:     {report.experiment}")
    if report.latest_request:
        lr = report.latest_request
        lines.append(f"Latest model:   {lr.get('model') or '—'}  ({lr.get('provider') or '—'})")
    runtime = ""
    if report.started_at and report.ended_at:
        runtime = f"{(report.ended_at - report.started_at):.0f}s"
    elif report.started_at:
        runtime = f"(ongoing, started {report.started_at:.0f})"
    lines.append(f"Runtime:        {runtime or 'n/a'}")
    lines.append("")
    lines.append(f"LLM requests:   {report.totals.api_requests}")
    lines.append(f"Tool calls:     {report.totals.tool_calls}")
    lines.append(f"Streaming:      {report.totals.streaming_requests}")
    lines.append("")
    lines.append("TOKEN ACCOUNTING (provider-measured unless labelled)")
    lines.append("-" * 60)
    lines.append(f"Provider prompt  {report.totals.prompt_tokens:>14,}")
    lines.append(f"Provider cached  {report.totals.cache_read_tokens:>14,}")
    fresh = max(0, report.totals.prompt_tokens - report.totals.cache_read_tokens)
    lines.append(f"Provider fresh   {fresh:>14,}")
    if report.totals.prompt_tokens:
        ratio = report.totals.cache_read_tokens / report.totals.prompt_tokens * 100
        lines.append(f"Cache hit        {ratio:>13.2f}%")
    lines.append(f"Output           {report.totals.output_tokens:>14,}")
    lines.append(f"Reasoning        {report.totals.reasoning_tokens:>14,}")
    lines.append("")

    if report.component_breakdown:
        lines.append("LOCAL ATTRIBUTION (estimated)")
        lines.append("-" * 60)
        for c in report.component_breakdown:
            lines.append(f"  {c.component:<22} {c.estimated_tokens:>12,}  ({c.percentage:5.1f}%)  conf={c.confidence:.2f}")
        if report.attribution_error_tokens is not None:
            err = report.attribution_error_tokens
            lines.append(f"  Attribution error vs provider: {err:+,} tokens")
        lines.append("")

    if report.tool_breakdown:
        lines.append("TOOL CONTEXT PRODUCERS")
        lines.append("-" * 60)
        for t in report.tool_breakdown:
            lines.append(f"  {t.category:<22} calls={t.calls:>5}  est_tokens={t.estimated_output_tokens:>10,}  ({t.percentage_of_calls:5.1f}%)")
        lines.append("")

    if report.totals.duration_s_api_avg:
        lines.append("PERFORMANCE")
        lines.append("-" * 60)
        if report.totals.ttft_s_avg is not None:
            lines.append(f"  Average TTFT:           {report.totals.ttft_s_avg:.2f}s")
        if report.totals.tps_avg is not None:
            lines.append(f"  Average TPS:            {report.totals.tps_avg:.1f}")
        lines.append(f"  Average request latency:{report.totals.duration_s_api_avg:.2f}s")
        lines.append("")

    if report.totals.projected_cost_usd is not None:
        lines.append("PROJECTED COST")
        lines.append("-" * 60)
        lines.append(f"  Actual provider cost:   $0.00 (not in DB)")
        lines.append(f"  Profile {pricing_profile or report.totals.projected_cost_profile}: ${report.totals.projected_cost_usd:.4f}")
        lines.append("")

    if report.findings:
        lines.append("INSIGHTS")
        lines.append("-" * 60)
        for f in report.findings[:10]:
            lines.append(f"  [{f['severality']}] {f['message']}  (conf={f['confidence']:.2f})")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _api_row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _pick_entry(table: PricingTable, rows: Iterable[Any]) -> Optional[PricingEntry]:
    seen_models: list[str] = []
    for r in rows:
        m = r["model"]
        if m and m not in seen_models:
            seen_models.append(m)
    for profile in table.profiles.values():
        for m in seen_models:
            entry = profile.lookup(m)
            if entry is not None:
                return entry
    return None


def _cost_against_entry(entry: Optional[PricingEntry], rows: Iterable[Any]) -> Optional[float]:
    if entry is None:
        return None
    total = 0.0
    for r in rows:
        cost = compute_cost(
            entry,
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_write_tokens=r["cache_write_tokens"],
            reasoning_tokens=r["reasoning_tokens"],
            request_count=1,
        )
        if cost is not None:
            total += cost
    return round(total, 6)


__all__ = [
    "SessionReport",
    "SessionTotals",
    "ComponentBreakdown",
    "ToolCategoryBreakdown",
    "build_report",
    "render_text",
]