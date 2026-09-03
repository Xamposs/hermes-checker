"""Report assembly.

The ``build_report`` function aggregates everything Hermes Checker has
recorded for a session into a single, printable structure used by both
the CLI (``hermes-checker report``) and the dashboard's SESSION view.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from hermes_checker.accounting import (
    PricingEntry,
    PricingTable,
    compute_cost,
)
from hermes_checker.storage import Database


# Context-size buckets for the by-bucket performance view (Issue 14).
_CONTEXT_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("0-32k", 0, 32_000),
    ("32-64k", 32_000, 64_000),
    ("64-128k", 64_000, 128_000),
    ("128-256k", 128_000, 256_000),
    ("256-512k", 256_000, 512_000),
    ("512k+", 512_000, 10**9),
)


def _percentile(sorted_samples: list[float], pct: float) -> float:
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    k = (len(sorted_samples) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = k - lo
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * frac


@dataclass
class SessionTotals:
    """Provider-measured totals for a session.

    ``cache_hit_ratio_weighted`` is the token-weighted cache hit ratio
    (Issue 13): ``sum(cache_read_tokens) / sum(prompt_tokens)`` over the
    session. ``cache_hit_ratio_mean`` is the simple per-request mean
    (kept for comparison). ``fresh_tokens`` is ``prompt - cache_read``;
    cache_write is preserved separately.
    """

    api_requests: int
    tool_calls: int
    prompt_tokens: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    fresh_tokens: int
    total_tokens: int
    duration_s_total: float
    duration_s_api_avg: Optional[float]
    duration_s_p50: Optional[float]
    duration_s_p95: Optional[float]
    ttft_s_avg: Optional[float]
    ttft_s_p50: Optional[float]
    ttft_s_p95: Optional[float]
    tps_avg: Optional[float]
    tps_p50: Optional[float]
    tps_p95: Optional[float]
    cache_hit_ratio_mean: Optional[float]
    cache_hit_ratio_weighted: Optional[float]
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
    provenance: str = "LOCALLY_ESTIMATED"


@dataclass
class ToolCategoryBreakdown:
    category: str
    calls: int
    estimated_output_tokens: int
    percentage_of_calls: float
    command_family: str = ""


@dataclass
class ProviderModelBreakdown:
    """Per-(provider, model) performance summary (Issue 14)."""

    provider: str
    model: str
    requests: int
    avg_ttft_s: Optional[float]
    avg_tps: Optional[float]
    avg_latency_s: Optional[float]
    cache_hit_ratio: Optional[float]
    total_prompt_tokens: int
    total_output_tokens: int


@dataclass
class ContextSizeBucket:
    """Per-context-size-bucket performance (Issue 14)."""

    label: str
    request_count: int
    avg_latency_s: Optional[float]
    avg_tps: Optional[float]
    cache_hit_ratio: Optional[float]


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
    attribution_coverage: Optional[float]
    provenance_notes: dict[str, str]
    latest_request: Optional[dict[str, Any]] = None
    by_provider_model: list[ProviderModelBreakdown] = field(default_factory=list)
    by_context_bucket: list[ContextSizeBucket] = field(default_factory=list)
    context_deltas: list[dict[str, Any]] = field(default_factory=list)
    skill_events_count: int = 0
    truncated_payloads: int = 0


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
    context_delta_rows = db.context_deltas_for_session(session_id)
    skill_event_rows = db.skill_events_for_session(session_id)

    # Provider-measured aggregates
    prompt_tokens = sum((r["prompt_tokens"] or 0) for r in api_rows)
    input_tokens = sum((r["input_tokens"] or 0) for r in api_rows)
    output_tokens = sum((r["output_tokens"] or 0) for r in api_rows)
    reasoning_tokens = sum((r["reasoning_tokens"] or 0) for r in api_rows)
    cache_read = sum((r["cache_read_tokens"] or 0) for r in api_rows)
    cache_write = sum((r["cache_write_tokens"] or 0) for r in api_rows)
    total_tokens = sum((r["total_tokens"] or 0) for r in api_rows)
    fresh_tokens = max(0, prompt_tokens - cache_read)
    truncated_payloads = sum(1 for r in api_rows if r["payload_truncated"])

    durations = [r["duration_s"] for r in api_rows if r["duration_s"] is not None]
    ttfts = [r["ttft_s"] for r in api_rows if r["ttft_s"] is not None]
    tps = [r["tokens_per_second"] for r in api_rows if r["tokens_per_second"] is not None]
    cache_ratios = [r["cache_hit_ratio"] for r in api_rows if r["cache_hit_ratio"] is not None]

    # Issue 14: P50/P95 percentiles for latency, TTFT, TPS.
    def _pct(values: list[float], pct: float) -> Optional[float]:
        if not values:
            return None
        return _percentile(sorted(values), pct)

    duration_s_p50 = _pct(durations, 50.0)
    duration_s_p95 = _pct(durations, 95.0)
    ttft_s_p50 = _pct(ttfts, 50.0)
    ttft_s_p95 = _pct(ttfts, 95.0)
    tps_p50 = _pct(tps, 50.0)
    tps_p95 = _pct(tps, 95.0)

    duration_api_avg = statistics.mean(durations) if durations else None
    ttft_avg = statistics.mean(ttfts) if ttfts else None
    tps_avg = statistics.mean(tps) if tps else None
    cache_hit_mean = statistics.mean(cache_ratios) if cache_ratios else None

    # Issue 13: TOKEN-WEIGHTED session cache hit ratio.
    cache_hit_weighted: Optional[float] = None
    if prompt_tokens > 0:
        cache_hit_weighted = cache_read / prompt_tokens

    streaming_requests = sum(1 for r in api_rows if r["streaming"])

    # Projected cost — apply the requested pricing profile if given.
    # MUST be computed before SessionTotals is constructed so the value
    # actually reaches the dataclass (Issue 15).
    provider_cost: Optional[float] = None
    projected_cost: Optional[float] = None
    projected_cost_profile: Optional[str] = None
    if pricing is not None and profile_name:
        table = pricing.get(profile_name) or pricing.get("default")
        if table is not None:
            entry = _pick_entry(table, api_rows)
            projected_cost = _cost_against_entry(entry, api_rows)
            projected_cost_profile = profile_name

    totals = SessionTotals(
        api_requests=len(api_rows),
        tool_calls=len(tool_rows),
        prompt_tokens=prompt_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        fresh_tokens=fresh_tokens,
        total_tokens=total_tokens,
        duration_s_total=sum(durations),
        duration_s_api_avg=duration_api_avg,
        duration_s_p50=duration_s_p50,
        duration_s_p95=duration_s_p95,
        ttft_s_avg=ttft_avg,
        ttft_s_p50=ttft_s_p50,
        ttft_s_p95=ttft_s_p95,
        tps_avg=tps_avg,
        tps_p50=tps_p50,
        tps_p95=tps_p95,
        cache_hit_ratio_mean=cache_hit_mean,
        cache_hit_ratio_weighted=cache_hit_weighted,
        provider_cost_usd=provider_cost,
        projected_cost_usd=projected_cost,
        projected_cost_profile=projected_cost_profile,
        streaming_requests=streaming_requests,
    )

    # Component breakdown — roll up per request, Issue 7 coverage
    component_rows: dict[str, dict[str, Any]] = {}
    for r in api_rows:
        for comp in db.prompt_components_for_request(r["id"]):
            comp_prov = comp["provenance"] if "provenance" in comp.keys() else "LOCALLY_ESTIMATED"
            c = component_rows.setdefault(
                comp["component"],
                {
                    "tokens": 0,
                    "method": comp["measurement_method"],
                    "provenance": comp_prov,
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                },
            )
            c["tokens"] += comp["estimated_tokens"] or 0
            c["confidence_sum"] += comp["confidence"] or 0
            c["confidence_count"] += 1

    local_total = sum(c["tokens"] for c in component_rows.values())
    component_total = local_total or 1
    component_breakdown = [
        ComponentBreakdown(
            component=name,
            estimated_tokens=int(c["tokens"]),
            percentage=(c["tokens"] / component_total * 100) if component_total else 0,
            measurement_method=str(c["method"]),
            confidence=c["confidence_sum"] / c["confidence_count"] if c["confidence_count"] else 0.5,
            provenance=str(c.get("provenance", "LOCALLY_ESTIMATED")),
        )
        for name, c in sorted(
            component_rows.items(),
            key=lambda kv: kv[1]["tokens"],
            reverse=True,
        )
    ]

    # Tool breakdown (Issue 9: command-family aware)
    tool_total = max(len(tool_rows), 1)
    cat_counts: dict[str, dict[str, Any]] = {}
    for tr in tool_rows:
        key = tr["category"] or "other"
        c = cat_counts.setdefault(key, {
            "calls": 0, "tokens": 0, "command_families": set(),
        })
        c["calls"] += 1
        c["tokens"] += tr["output_tokens_est"] or 0
        if tr["command_family"]:
            c["command_families"].add(tr["command_family"])
    tool_breakdown = [
        ToolCategoryBreakdown(
            category=name,
            calls=v["calls"],
            estimated_output_tokens=v["tokens"],
            percentage_of_calls=v["calls"] / tool_total * 100,
            command_family=", ".join(sorted(v["command_families"])) if v["command_families"] else "",
        )
        for name, v in sorted(
            cat_counts.items(), key=lambda kv: kv[1]["calls"], reverse=True
        )
    ]

    # Attribution coverage (Issue 7) — explicit un-attributed gap.
    attribution_error: Optional[int] = None
    attribution_coverage: Optional[float] = None
    if prompt_tokens:
        attribution_error = int(local_total) - prompt_tokens
        attribution_coverage = max(0.0, min(1.0, local_total / prompt_tokens))

    provenance_notes = {
        "prompt_tokens": "PROVIDER_MEASURED (Hermes canonicalises the provider's usage dict; if missing, left NULL with provenance=UNAVAILABLE).",
        "input_tokens": "PROVIDER_MEASURED.",
        "output_tokens": "PROVIDER_MEASURED.",
        "reasoning_tokens": "PROVIDER_MEASURED when the provider reports them separately; otherwise UNKNOWN.",
        "cache_read_tokens": "PROVIDER_MEASURED when the provider reports cache reads; many free providers do not, so this often is 0.",
        "cache_write_tokens": "PROVIDER_MEASURED when the provider reports cache writes.",
        "total_tokens": "LOCALLY_CALCULATED as prompt + output.",
        "fresh_tokens": "LOCALLY_CALCULATED as prompt - cache_read.",
        "cache_hit_ratio_weighted": "LOCALLY_CALCULATED as sum(cache_read) / sum(prompt) over the session (Issue 13).",
        "component_breakdown": "LOCALLY_ESTIMATED. When a static_prompt_snapshots row is present, its HERMES_NATIVE_ESTIMATE numbers can be cross-checked against this breakdown.",
        "tool_breakdown": "LOCALLY_ESTIMATED (tokens/4 fallback unless tiktoken is installed).",
        "context_deltas": "LOCALLY_ATTRIBUTED — sum of per-component token changes between consecutive API requests in the session.",
    }

    latest = None
    if api_rows:
        last = api_rows[-1]
        latest = _api_row_to_dict(last)

    by_provider_model = _by_provider_model(api_rows)
    by_context_bucket = _by_context_bucket(api_rows)

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
        attribution_coverage=attribution_coverage,
        provenance_notes=provenance_notes,
        latest_request=latest,
        by_provider_model=by_provider_model,
        by_context_bucket=by_context_bucket,
        context_deltas=[dict(r) for r in context_delta_rows],
        skill_events_count=len(skill_event_rows),
        truncated_payloads=truncated_payloads,
    )


def _by_provider_model(api_rows: Sequence[Any]) -> list[ProviderModelBreakdown]:
    out: dict[tuple[str, str], list[Any]] = {}
    for r in api_rows:
        key = (r["provider"] or "unknown", r["model"] or "unknown")
        out.setdefault(key, []).append(r)
    breakdown: list[ProviderModelBreakdown] = []
    for (provider, model), rows in out.items():
        durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
        ttfts = [r["ttft_s"] for r in rows if r["ttft_s"] is not None]
        tps = [r["tokens_per_second"] for r in rows if r["tokens_per_second"] is not None]
        ratios = [r["cache_hit_ratio"] for r in rows if r["cache_hit_ratio"] is not None]
        breakdown.append(ProviderModelBreakdown(
            provider=provider,
            model=model,
            requests=len(rows),
            avg_ttft_s=statistics.mean(ttfts) if ttfts else None,
            avg_tps=statistics.mean(tps) if tps else None,
            avg_latency_s=statistics.mean(durations) if durations else None,
            cache_hit_ratio=statistics.mean(ratios) if ratios else None,
            total_prompt_tokens=sum((r["prompt_tokens"] or 0) for r in rows),
            total_output_tokens=sum((r["output_tokens"] or 0) for r in rows),
        ))
    breakdown.sort(key=lambda d: (-d.requests, d.provider, d.model))
    return breakdown


def _by_context_bucket(api_rows: Sequence[Any]) -> list[ContextSizeBucket]:
    out: dict[str, list[Any]] = {label: [] for label, _, _ in _CONTEXT_BUCKETS}
    for r in api_rows:
        prompt = r["prompt_tokens"] or 0
        for label, lo, hi in _CONTEXT_BUCKETS:
            if lo <= prompt < hi:
                out[label].append(r)
                break
    buckets: list[ContextSizeBucket] = []
    for label, _, _ in _CONTEXT_BUCKETS:
        rows = out[label]
        if not rows:
            continue
        durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
        tps = [r["tokens_per_second"] for r in rows if r["tokens_per_second"] is not None]
        ratios = [r["cache_hit_ratio"] for r in rows if r["cache_hit_ratio"] is not None]
        buckets.append(ContextSizeBucket(
            label=label,
            request_count=len(rows),
            avg_latency_s=statistics.mean(durations) if durations else None,
            avg_tps=statistics.mean(tps) if tps else None,
            cache_hit_ratio=statistics.mean(ratios) if ratios else None,
        ))
    return buckets


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
    if report.truncated_payloads:
        lines.append(
            f"Truncated payload(s): {report.truncated_payloads}  "
            "(Hermes capped the visible hook payload; "
            "attribution for those requests is suppressed)"
        )
    if report.skill_events_count:
        lines.append(f"Skill events:   {report.skill_events_count}")
    lines.append("")
    lines.append("TOKEN ACCOUNTING (provider-measured unless labelled)")
    lines.append("-" * 60)
    lines.append(f"Provider prompt  {report.totals.prompt_tokens:>14,}")
    lines.append(f"Provider cached  {report.totals.cache_read_tokens:>14,}")
    lines.append(f"Provider fresh   {report.totals.fresh_tokens:>14,}")
    lines.append(f"Provider cached write {report.totals.cache_write_tokens:>9,}")
    if report.totals.prompt_tokens:
        ratio = report.totals.cache_read_tokens / report.totals.prompt_tokens * 100
        lines.append(f"Cache hit (weighted) {ratio:>10.2f}%  (Issue 13)")
        if report.totals.cache_hit_ratio_mean is not None:
            lines.append(
                f"Cache hit (mean)     "
                f"{(report.totals.cache_hit_ratio_mean or 0) * 100:>10.2f}%  (unweighted; for comparison)"
            )
    lines.append(f"Output           {report.totals.output_tokens:>14,}")
    lines.append(f"Reasoning        {report.totals.reasoning_tokens:>14,}")
    lines.append("")

    if report.component_breakdown:
        lines.append("LOCAL ATTRIBUTION (estimated; HERMES_NATIVE_ESTIMATE when a snapshot is joined)")
        lines.append("-" * 60)
        for c in report.component_breakdown:
            lines.append(
                f"  {c.component:<22} {c.estimated_tokens:>12,}  "
                f"({c.percentage:5.1f}%)  conf={c.confidence:.2f}  prov={c.provenance}"
            )
        if report.attribution_error_tokens is not None and report.totals.prompt_tokens:
            err = report.attribution_error_tokens
            cov = report.attribution_coverage or 0.0
            lines.append(
                f"  Attribution error vs provider: {err:+,} tokens"
            )
            lines.append(
                f"  Coverage:  {cov*100:6.2f}%  "
                f"({report.totals.prompt_tokens:,} provider prompt tokens total)"
            )
        lines.append("")

    if report.tool_breakdown:
        lines.append("TOOL CONTEXT PRODUCERS (Issue 9: command-aware)")
        lines.append("-" * 60)
        for t in report.tool_breakdown:
            extra = f"  cmd={t.command_family}" if t.command_family else ""
            lines.append(
                f"  {t.category:<22} calls={t.calls:>5}  "
                f"est_tokens={t.estimated_output_tokens:>10,}  "
                f"({t.percentage_of_calls:5.1f}%){extra}"
            )
        lines.append("")

    if report.totals.duration_s_api_avg:
        lines.append("PERFORMANCE (Issue 14: P50/P95)")
        lines.append("-" * 60)
        if report.totals.ttft_s_avg is not None:
            ttft_str = f"  TTFT  avg={report.totals.ttft_s_avg:.2f}s"
            if report.totals.ttft_s_p50 is not None:
                ttft_str += f"  p50={report.totals.ttft_s_p50:.2f}s"
            if report.totals.ttft_s_p95 is not None:
                ttft_str += f"  p95={report.totals.ttft_s_p95:.2f}s"
            lines.append(ttft_str)
        if report.totals.tps_avg is not None:
            tps_str = f"  TPS   avg={report.totals.tps_avg:.1f}"
            if report.totals.tps_p50 is not None:
                tps_str += f"  p50={report.totals.tps_p50:.1f}"
            if report.totals.tps_p95 is not None:
                tps_str += f"  p95={report.totals.tps_p95:.1f}"
            lines.append(tps_str)
        if report.totals.duration_s_p50 is not None:
            lines.append(
                f"  Latency  avg={report.totals.duration_s_api_avg:.2f}s"
                f"  p50={report.totals.duration_s_p50:.2f}s"
                f"  p95={report.totals.duration_s_p95:.2f}s"
            )
        else:
            lines.append(f"  Latency  avg={report.totals.duration_s_api_avg:.2f}s")
        lines.append("")

    if report.by_provider_model:
        lines.append("BY (PROVIDER, MODEL)")
        lines.append("-" * 60)
        for b in report.by_provider_model:
            avg_ttft = f"{b.avg_ttft_s:.2f}s" if b.avg_ttft_s is not None else "—"
            avg_tps = f"{b.avg_tps:.1f}" if b.avg_tps is not None else "—"
            avg_lat = f"{b.avg_latency_s:.2f}s" if b.avg_latency_s is not None else "—"
            ratio = (
                f"{(b.cache_hit_ratio or 0) * 100:5.1f}%"
                if b.cache_hit_ratio is not None else "—"
            )
            lines.append(
                f"  {b.provider:<14} {b.model:<32} reqs={b.requests:>3}  "
                f"lat={avg_lat}  ttft={avg_ttft}  tps={avg_tps}  cache={ratio}"
            )
        lines.append("")

    if report.by_context_bucket:
        lines.append("BY CONTEXT-SIZE BUCKET")
        lines.append("-" * 60)
        for b in report.by_context_bucket:
            avg_lat = (
                f"{b.avg_latency_s:.2f}s" if b.avg_latency_s is not None else "—"
            )
            avg_tps = f"{b.avg_tps:.1f}" if b.avg_tps is not None else "—"
            ratio = (
                f"{(b.cache_hit_ratio or 0) * 100:5.1f}%"
                if b.cache_hit_ratio is not None else "—"
            )
            lines.append(
                f"  {b.label:<10} reqs={b.request_count:>3}  lat={avg_lat}  "
                f"tps={avg_tps}  cache={ratio}"
            )
        lines.append("")

    if report.context_deltas:
        lines.append("CONTEXT DELTAS (Issue 12 — between consecutive API requests)")
        lines.append("-" * 60)
        for d in report.context_deltas[:8]:
            try:
                import json as _json
                contrib_list = (
                    _json.loads(d["contributors_json"])
                    if isinstance(d.get("contributors_json"), str)
                    else (d.get("contributors_json") or [])
                )
            except Exception:
                contrib_list = []
            top = ", ".join(
                f"{c.get('component', '?')} {c.get('tokens', 0):+,}"
                for c in (contrib_list or [])[:3]
            )
            coverage = d.get("coverage")
            cov_str = f"{coverage*100:5.1f}%" if coverage is not None else "—"
            lines.append(
                f"  t={d.get('detected_at', 0):.0f}  "
                f"delta={d.get('provider_delta_tokens', 0):+,}  "
                f"explained={d.get('explained_tokens', 0):+,}  "
                f"cov={cov_str}  {top}"
            )
        lines.append("")

    if report.totals.projected_cost_usd is not None:
        lines.append("PROJECTED COST (Issue 15: wired into SessionTotals)")
        lines.append("-" * 60)
        lines.append(
            "  Actual provider cost:   $0.00 (not in DB; "
            "Hermes does not return an invoice line)"
        )
        profile_label = pricing_profile or report.totals.projected_cost_profile
        lines.append(
            f"  Profile {profile_label}:   ${report.totals.projected_cost_usd:.4f}"
        )
        lines.append("")

    if report.findings:
        lines.append("INSIGHTS (rule-based evidence only — never claim 'waste')")
        lines.append("-" * 60)
        for f in report.findings[:10]:
            severity = f.get("severity", "?")
            lines.append(
                f"  [{severity}] {f.get('message', '')}  "
                f"(conf={float(f.get('confidence', 0)):.2f})"
            )
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
    "ProviderModelBreakdown",
    "ContextSizeBucket",
    "build_report",
    "render_text",
]