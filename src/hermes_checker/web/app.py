"""FastAPI application for the Hermes Checker dashboard.

The dashboard reads from a read-only connection to the same SQLite
database the collector writes.  We never write from the dashboard.

Endpoints
---------

- ``GET  /``                                 — the SPA shell (HTML)
- ``GET  /static/<file>``                     — CSS / JS assets
- ``GET  /api/health``                        — liveness probe
- ``GET  /api/sessions``                      — list recent sessions
- ``GET  /api/sessions/<id>``                 — full report for one session
- ``GET  /api/sessions/<id>/requests``        — API request timeline
- ``GET  /GET  /api/sessions/<id>/tools``     — tool call timeline
- ``GET  /api/sessions/<id>/components``      — component attribution rows
- ``GET  /api/analytics``                     — aggregated metrics
- ``GET  /api/insights``                     — most recent findings
- ``GET  /api/live``                          — current-session snapshot
- ``POST /api/analyze/<session_id>``          — run the analyzer on demand
- ``GET  /api/pricing``                       — list loaded pricing profiles
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

try:  # FastAPI is optional
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    _FASTAPI = True
except Exception:  # pragma: no cover
    FastAPI = None  # type: ignore
    HTTPException = None  # type: ignore
    Request = None  # type: ignore
    FileResponse = None  # type: ignore
    HTMLResponse = None  # type: ignore
    JSONResponse = None  # type: ignore
    StaticFiles = None  # type: ignore
    _FASTAPI = False

from hermes_checker.storage import Database, DatabasePaths

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_FILE = _STATIC_DIR / "index.html"


def build_app(
    database: Optional[Database] = None,
    *,
    paths: Optional[DatabasePaths] = None,
) -> Any:
    """Construct (but do not run) the FastAPI app."""
    if not _FASTAPI:
        raise RuntimeError(
            "FastAPI is not installed. Run: pip install hermes-checker[web]"
        )

    if database is None:
        paths = paths or DatabasePaths.default()
        database = Database(paths, readonly=True)

    app = FastAPI(
        title="Hermes Checker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:  # noqa: D401
        if not _INDEX_FILE.exists():
            return HTMLResponse("<h1>Hermes Checker</h1><p>Static assets missing.</p>")
        return HTMLResponse(_INDEX_FILE.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hermes-checker",
            "version": "0.1.0",
            "schema_version": database.schema_version,
            "now": time.time(),
        }

    @app.get("/api/sessions")
    def list_sessions(limit: int = 50) -> dict[str, Any]:
        rows = database.sessions(limit=limit)
        return {
            "sessions": [_session_summary(r) for r in rows],
        }

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str) -> dict[str, Any]:
        if not database.session(session_id):
            raise HTTPException(404, "Unknown session")
        requests = [_api_request_summary(r) for r in database.api_requests_for_session(session_id)]
        tools = [_tool_summary(r) for r in database.tool_calls_for_session(session_id)]
        components = []
        for r in database.api_requests_for_session(session_id):
            for c in database.prompt_components_for_request(r["id"]):
                components.append({
                    "api_request_id": r["id"],
                    "component": c["component"],
                    "characters": c["characters"],
                    "estimated_tokens": c["estimated_tokens"],
                    "measurement_method": c["measurement_method"],
                    "confidence": c["confidence"],
                })
        return {
            "session": _session_full(database.session(session_id)),
            "api_requests": requests,
            "tool_calls": tools,
            "components": components,
            "totals": _totals_from_rows(requests),
        }

    @app.get("/api/sessions/{session_id}/requests")
    def session_requests(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "requests": [_api_request_summary(r)
                         for r in database.api_requests_for_session(session_id)],
        }

    @app.get("/api/sessions/{session_id}/tools")
    def session_tools(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "tool_calls": [_tool_summary(r)
                           for r in database.tool_calls_for_session(session_id)],
        }

    @app.get("/api/sessions/{session_id}/components")
    def session_components(session_id: str) -> dict[str, Any]:
        out = []
        for r in database.api_requests_for_session(session_id):
            for c in database.prompt_components_for_request(r["id"]):
                out.append({
                    "api_request_id": r["id"],
                    "started_at": r["started_at"],
                    "component": c["component"],
                    "estimated_tokens": c["estimated_tokens"],
                    "percentage": None,
                    "measurement_method": c["measurement_method"],
                })
        return {"session_id": session_id, "components": out}

    @app.get("/api/analytics")
    def analytics(window: str = "7d") -> dict[str, Any]:
        # Build a simple windowed aggregate from sessions.
        # We don't materialise this in V1; rely on the sessions + requests
        # tables and compute in Python because the dataset is small.
        now = time.time()
        windows = {"1d": 86400, "7d": 86400 * 7, "30d": 86400 * 30, "all": None}
        cutoff = windows.get(window, windows["7d"])
        if cutoff is not None:
            cutoff = now - cutoff

        sessions = database.sessions(limit=1000)
        if cutoff is not None:
            sessions = [s for s in sessions if s["started_at"] >= cutoff]

        agg = {
            "sessions": len(sessions),
            "api_requests": 0,
            "tool_calls": 0,
            "provider_prompt_tokens": 0,
            "provider_cached_tokens": 0,
            "provider_output_tokens": 0,
            "provider_reasoning_tokens": 0,
            "cache_hit_ratio_sum": 0.0,
            "cache_hit_ratio_count": 0,
            "tps_sum": 0.0,
            "tps_count": 0,
            "ttft_sum": 0.0,
            "ttft_count": 0,
        }
        for s in sessions:
            for r in database.api_requests_for_session(s["session_id"]):
                agg["api_requests"] += 1
                agg["provider_prompt_tokens"] += r["prompt_tokens"] or 0
                agg["provider_cached_tokens"] += r["cache_read_tokens"] or 0
                agg["provider_output_tokens"] += r["output_tokens"] or 0
                agg["provider_reasoning_tokens"] += r["reasoning_tokens"] or 0
                if r["cache_hit_ratio"] is not None:
                    agg["cache_hit_ratio_sum"] += r["cache_hit_ratio"]
                    agg["cache_hit_ratio_count"] += 1
                if r["tokens_per_second"] is not None:
                    agg["tps_sum"] += r["tokens_per_second"]
                    agg["tps_count"] += 1
                if r["ttft_s"] is not None:
                    agg["ttft_sum"] += r["ttft_s"]
                    agg["ttft_count"] += 1
            agg["tool_calls"] += len(database.tool_calls_for_session(s["session_id"]))

        if agg["cache_hit_ratio_count"]:
            agg["cache_hit_ratio_avg"] = agg["cache_hit_ratio_sum"] / agg["cache_hit_ratio_count"]
        else:
            agg["cache_hit_ratio_avg"] = None
        if agg["tps_count"]:
            agg["tps_avg"] = agg["tps_sum"] / agg["tps_count"]
        else:
            agg["tps_avg"] = None
        if agg["ttft_count"]:
            agg["ttft_avg"] = agg["ttft_sum"] / agg["ttft_count"]
        else:
            agg["ttft_avg"] = None
        return {"window": window, "totals": agg}

    @app.get("/api/insights")
    def insights(session_id: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
        rows = database.findings(session_id=session_id, limit=limit)
        return {
            "insights": [dict(r) for r in rows],
        }

    @app.get("/api/live")
    def live() -> dict[str, Any]:
        sessions = database.sessions(limit=1)
        if not sessions:
            return {"session": None, "totals": {}, "events": []}
        sid = sessions[0]["session_id"]
        requests = [_api_request_summary(r) for r in database.api_requests_for_session(sid)]
        events = [
            dict(r) for r in database.events(session_id=sid, limit=10)
        ]
        return {
            "session": _session_full(sessions[0]),
            "totals": _totals_from_rows(requests),
            "events": events,
        }

    @app.post("/api/analyze/{session_id}")
    def trigger_analyze(session_id: str) -> dict[str, Any]:
        if not database.session(session_id):
            raise HTTPException(404, "Unknown session")
        from hermes_checker.analysis import Analyzer
        from hermes_checker.collector.config import default_collector_config
        analyzer = Analyzer(database, default_collector_config())
        findings = analyzer.analyze_session(session_id)
        return {
            "session_id": session_id,
            "findings": [f.__dict__ for f in findings],
        }

    @app.get("/api/sessions/{session_id}/report")
    def session_report(session_id: str,
                      pricing_file: Optional[str] = None,
                      profile: Optional[str] = None) -> dict[str, Any]:
        """Full :class:`SessionReport` for one session (V1.1 view).

        Includes the per-component attribution, the per-tool breakdown,
        the per-(provider, model) performance breakdown, the per-context-
        size bucket breakdown, the locally-attributed context deltas
        between consecutive API requests, and the projected cost (when
        a pricing profile is supplied).
        """
        if not database.session(session_id):
            raise HTTPException(404, "Unknown session")
        from hermes_checker.reporting import build_report
        pricing = None
        if pricing_file:
            from pathlib import Path
            from hermes_checker.accounting import load_pricing_profile
            try:
                table = load_pricing_profile(Path(pricing_file))
                pricing = {"default": table}
            except Exception as exc:
                raise HTTPException(400, f"Failed to load pricing file: {exc}")
        report = build_report(database, session_id, pricing=pricing, profile_name=profile)
        return {
            "session_id": report.session_id,
            "profile": report.profile,
            "platform": report.platform,
            "started_at": report.started_at,
            "ended_at": report.ended_at,
            "experiment": report.experiment,
            "totals": _serialize_totals(report.totals),
            "component_breakdown": [
                {
                    "component": c.component,
                    "estimated_tokens": c.estimated_tokens,
                    "percentage": c.percentage,
                    "measurement_method": c.measurement_method,
                    "confidence": c.confidence,
                    "provenance": c.provenance,
                }
                for c in report.component_breakdown
            ],
            "tool_breakdown": [
                {
                    "category": t.category,
                    "calls": t.calls,
                    "estimated_output_tokens": t.estimated_output_tokens,
                    "percentage_of_calls": t.percentage_of_calls,
                    "command_family": t.command_family,
                }
                for t in report.tool_breakdown
            ],
            "by_provider_model": [
                {
                    "provider": b.provider,
                    "model": b.model,
                    "requests": b.requests,
                    "avg_ttft_s": b.avg_ttft_s,
                    "avg_tps": b.avg_tps,
                    "avg_latency_s": b.avg_latency_s,
                    "cache_hit_ratio": b.cache_hit_ratio,
                    "total_prompt_tokens": b.total_prompt_tokens,
                    "total_output_tokens": b.total_output_tokens,
                }
                for b in report.by_provider_model
            ],
            "by_context_bucket": [
                {
                    "label": b.label,
                    "request_count": b.request_count,
                    "avg_latency_s": b.avg_latency_s,
                    "avg_tps": b.avg_tps,
                    "cache_hit_ratio": b.cache_hit_ratio,
                }
                for b in report.by_context_bucket
            ],
            "context_deltas": [
                {
                    "session_id": d["session_id"],
                    "previous_api_request_id": d["previous_api_request_id"],
                    "current_api_request_id": d["current_api_request_id"],
                    "provider_delta_tokens": d["provider_delta_tokens"],
                    "explained_tokens": d["explained_tokens"],
                    "unexplained_tokens": d["unexplained_tokens"],
                    "coverage": d["coverage"],
                    "contributors": _safe_json(d["contributors_json"]) or [],
                    "detected_at": d["detected_at"],
                    "confidence": d["confidence"],
                }
                for d in report.context_deltas
            ],
            "attribution_coverage": report.attribution_coverage,
            "attribution_error_tokens": report.attribution_error_tokens,
            "truncated_payloads": report.truncated_payloads,
            "skill_events_count": report.skill_events_count,
            "findings": report.findings,
            "provenance_notes": report.provenance_notes,
        }

    @app.get("/api/sessions/{session_id}/snapshots")
    def session_snapshots(session_id: str) -> dict[str, Any]:
        """Latest static prompt snapshot for the session (or globally)."""
        snap = database.latest_snapshot()
        if snap is None:
            return {"session_id": session_id, "snapshot": None}
        return {
            "session_id": session_id,
            "snapshot": {
                "id": snap["id"],
                "taken_at": snap["taken_at"],
                "hermes_version": snap["hermes_version"],
                "platform": snap["platform"],
                "model": snap["model"],
                "system_prompt_tokens_est": snap["system_prompt_tokens_est"],
                "stable_tokens_est": snap["stable_tokens_est"],
                "context_tokens_est": snap["context_tokens_est"],
                "volatile_tokens_est": snap["volatile_tokens_est"],
                "skills_index_tokens_est": snap["skills_index_tokens_est"],
                "memory_tokens_est": snap["memory_tokens_est"],
                "user_profile_tokens_est": snap["user_profile_tokens_est"],
                "tools_json_tokens_est": snap["tools_json_tokens_est"],
                "mcp_schemas_tokens_est": snap["mcp_schemas_tokens_est"],
                "subagent_defs_tokens_est": snap["subagent_defs_tokens_est"],
                "hermes_native": bool(snap["hermes_native"]),
                "tokenizer_method": snap["tokenizer_method"],
            },
        }

    @app.get("/api/sessions/{session_id}/skill-events")
    def session_skill_events(session_id: str) -> dict[str, Any]:
        events = database.skill_events_for_session(session_id)
        return {
            "session_id": session_id,
            "events": [dict(r) for r in events],
        }

    @app.get("/api/pricing")
    def pricing_list() -> dict[str, Any]:
        rows = database.pricing()
        return {"pricing": [dict(r) for r in rows]}

    return app


def run_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    database: Optional[Database] = None,
) -> None:
    """Run the dashboard with uvicorn."""
    try:
        import uvicorn  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "uvicorn is not installed. Run: pip install hermes-checker[web]"
        ) from exc
    app = build_app(database=database)
    uvicorn.run(app, host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# row → JSON helpers
# ---------------------------------------------------------------------------


def _session_summary(row: Any) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "profile": row["profile"],
        "platform": row["platform"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "experiment": row["experiment"],
    }


def _session_full(row: Any) -> dict[str, Any]:
    return _session_summary(row) | {
        "metadata": _safe_json(row["metadata_json"]),
    }


def _api_request_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_s": row["duration_s"],
        "ttft_s": row["ttft_s"],
        "tokens_per_second": row["tokens_per_second"],
        "cache_hit_ratio": row["cache_hit_ratio"],
        "provider": row["provider"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "total_tokens": row["total_tokens"],
        "streaming": bool(row["streaming"]) if row["streaming"] is not None else None,
        "finish_reason": row["finish_reason"],
        "api_call_count": row["api_call_count"],
        "messages_count": row["messages_count"],
    }


def _tool_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "duration_ms": row["duration_ms"],
        "tool_name": row["tool_name"],
        "category": row["category"],
        "status": row["status"],
        "output_chars": row["output_chars"],
        "output_tokens_est": row["output_tokens_est"],
        "output_truncated": row["output_truncated"],
        "exit_code": row["exit_code"],
        "args_summary": row["args_summary"],
        "output_hash": row["output_hash"],
    }


def _totals_from_rows(requests: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = sum(r["prompt_tokens"] or 0 for r in requests)
    cached = sum(r["cache_read_tokens"] or 0 for r in requests)
    output = sum(r["output_tokens"] or 0 for r in requests)
    reasoning = sum(r["reasoning_tokens"] or 0 for r in requests)
    total = sum(r["total_tokens"] or 0 for r in requests)
    ratios = [r["cache_hit_ratio"] for r in requests if r["cache_hit_ratio"] is not None]
    weighted = (sum(r["weight_cached"] or 0 for r in requests) / prompt) if prompt else None
    return {
        "api_requests": len(requests),
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "fresh_tokens": max(0, prompt - cached),
        "cache_hit_ratio": (sum(ratios) / len(ratios)) if ratios else None,
        "cache_hit_ratio_weighted": weighted,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def _serialize_totals(t: Any) -> dict[str, Any]:
    """Turn a :class:`SessionTotals` into a JSON-friendly dict."""
    return {
        "api_requests": t.api_requests,
        "tool_calls": t.tool_calls,
        "prompt_tokens": t.prompt_tokens,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "reasoning_tokens": t.reasoning_tokens,
        "cache_read_tokens": t.cache_read_tokens,
        "cache_write_tokens": t.cache_write_tokens,
        "fresh_tokens": t.fresh_tokens,
        "total_tokens": t.total_tokens,
        "duration_s_total": t.duration_s_total,
        "duration_s_api_avg": t.duration_s_api_avg,
        "duration_s_p50": t.duration_s_p50,
        "duration_s_p95": t.duration_s_p95,
        "ttft_s_avg": t.ttft_s_avg,
        "ttft_s_p50": t.ttft_s_p50,
        "ttft_s_p95": t.ttft_s_p95,
        "tps_avg": t.tps_avg,
        "tps_p50": t.tps_p50,
        "tps_p95": t.tps_p95,
        "cache_hit_ratio_mean": t.cache_hit_ratio_mean,
        "cache_hit_ratio_weighted": t.cache_hit_ratio_weighted,
        "provider_cost_usd": t.provider_cost_usd,
        "projected_cost_usd": t.projected_cost_usd,
        "projected_cost_profile": t.projected_cost_profile,
        "streaming_requests": t.streaming_requests,
    }


def _safe_json(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


__all__ = ["build_app", "run_dashboard"]