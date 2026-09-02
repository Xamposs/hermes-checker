"""CLI entry point.

The ``hermes-checker`` command is a thin wrapper around the underlying
modules.  It is intentionally text-only so it can be used from any
terminal and so the help text fits in a few screens.

Commands
--------

- ``doctor``                  — verify Hermes + integration
- ``status``                  — short status summary
- ``dashboard``               — start the local FastAPI dashboard
- ``report``                  — print a session report
- ``sessions``                — list observed sessions
- ``export``                  — export a session as JSON / CSV
- ``pricing``                 — load and inspect a pricing YAML file
- ``install``                 — install the Hermes user plugin
- ``uninstall``               — undo the install
- ``analyze``                 — run the analyzer on demand
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from hermes_checker import __version__
from hermes_checker.storage import Database, DatabasePaths


def _open_database(args: argparse.Namespace) -> Database:
    paths = DatabasePaths.from_path(Path(args.db).expanduser()) if getattr(args, "db", None) else DatabasePaths.default()
    return Database(paths, readonly=not getattr(args, "write", False))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if not args.command:
        parser.print_help()
        return 0
    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args) or 0)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-checker",
        description="Hermes Checker — local observability for Hermes Agent / Hermes Desktop.",
    )
    p.add_argument("--db", help="Override path to the SQLite database.", default=None)
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("doctor", help="Verify the installation and integration.")
    sub.add_parser("status", help="One-line status summary.")

    dash = sub.add_parser("dashboard", help="Start the local dashboard.")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)

    report = sub.add_parser("report", help="Print a session report.")
    report.add_argument("--session", required=True, help="Session id to report on.")
    report.add_argument("--today", action="store_true", help="Use today's latest session.")
    report.add_argument("--pricing-profile", default=None, help="Project cost using this profile.")
    report.add_argument("--pricing-file", default=None, help="Pricing YAML file.")

    sessions = sub.add_parser("sessions", help="List observed sessions.")
    sessions.add_argument("--limit", type=int, default=20)

    export = sub.add_parser("export", help="Export session metrics.")
    export.add_argument("--session", required=True)
    export.add_argument("--format", choices=["json", "csv"], default="json")
    export.add_argument("--out", default="-", help="Output path ('-' = stdout).")

    pricing = sub.add_parser("pricing", help="Inspect a pricing YAML file.")
    pricing.add_argument("path", help="Path to a pricing YAML file.")
    pricing.add_argument("--profile", default=None, help="Print entries for a single profile.")

    install = sub.add_parser("install", help="Install the Hermes user plugin.")
    install.add_argument("--hermes-home", default=None, help="Override Hermes home (default: %%USERPROFILE%%\\.hermes or %%LOCALAPPDATA%%\\hermes).")
    install.add_argument("--dry-run", action="store_true")

    sub.add_parser("uninstall", help="Undo a previous install.")

    analyze = sub.add_parser("analyze", help="Run the analyzer on a session.")
    analyze.add_argument("--session", required=True)

    return p


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .install import diagnose
    return diagnose(verbose=True)


def _cmd_status(args: argparse.Namespace) -> int:
    db = _open_database(args)
    sessions = db.sessions(limit=5)
    if not sessions:
        print("Hermes Checker — no sessions yet.")
        return 0
    latest = sessions[0]
    requests = db.api_requests_for_session(latest["session_id"])
    tools = db.tool_calls_for_session(latest["session_id"])
    print(f"Hermes Checker {__version__}")
    print(f"  Database:      {db.paths.database}")
    print(f"  Schema:        v{db.schema_version}")
    print(f"  Sessions:      {len(db.sessions(limit=10_000))}")
    print(f"  Latest:        {latest['session_id']}  (started {latest['started_at']:.0f})")
    print(f"  LLM requests:  {len(requests)}")
    print(f"  Tool calls:    {len(tools)}")
    if requests:
        last = requests[-1]
        prompt = last["prompt_tokens"] or 0
        cached = last["cache_read_tokens"] or 0
        out = last["output_tokens"] or 0
        if prompt:
            ratio = cached / prompt * 100
            print(f"  Last request:  prompt={prompt:,}  cached={cached:,} ({ratio:.1f}%)  output={out:,}")
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    db = _open_database(args)
    from hermes_checker.web import run_dashboard
    run_dashboard(host=args.host, port=args.port, database=db)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    db = _open_database(args)
    pricing = _load_pricing(args)
    from hermes_checker.reporting import build_report, render_text
    session_id = args.session
    if args.today and session_id == "today":
        sessions = db.sessions(limit=1)
        if not sessions:
            print("No sessions today.")
            return 1
        session_id = sessions[0]["session_id"]
    report = build_report(db, session_id, pricing=pricing, profile_name=args.pricing_profile)
    print(render_text(report, pricing_profile=args.pricing_profile))
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    db = _open_database(args)
    sessions = db.sessions(limit=args.limit)
    if not sessions:
        print("(no sessions)")
        return 0
    for s in sessions:
        started = s["started_at"]
        print(f"{s['session_id']:<40}  started={started:.0f}  profile={s['profile'] or '-'}  experiment={s['experiment'] or '-'}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    db = _open_database(args)
    sid = args.session
    rows = db.api_requests_for_session(sid)
    if args.format == "json":
        payload = {
            "session_id": sid,
            "exported_at": __import__("time").time(),
            "api_requests": [_api_row_to_dict(r) for r in rows],
            "tool_calls": [_tool_row_to_dict(r) for r in db.tool_calls_for_session(sid)],
        }
        out_text = json.dumps(payload, indent=2, default=str)
    else:
        buf = io.StringIO()
        if rows:
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r[k] for k in fieldnames})
        out_text = buf.getvalue()
    if args.out == "-":
        print(out_text)
    else:
        Path(args.out).write_text(out_text, encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


def _cmd_pricing(args: argparse.Namespace) -> int:
    from hermes_checker.accounting import load_pricing_profile
    table = load_pricing_profile(Path(args.path))
    if args.profile:
        profile = table.get(args.profile)
        if not profile:
            print(f"Profile {args.profile} not found. Available: {table.profile_names()}")
            return 1
        for model, entry in profile.models.items():
            print(f"{model}: input=${entry.input_per_million_usd}/M output=${entry.output_per_million_usd}/M")
    else:
        for name in table.profile_names():
            prof = table.get(name)
            print(f"Profile '{name}' — {len(prof.models)} model(s)")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from .install import install_plugin
    return install_plugin(
        hermes_home=Path(args.hermes_home).expanduser() if args.hermes_home else None,
        dry_run=args.dry_run,
    )


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from .install import uninstall_plugin
    return uninstall_plugin()


def _cmd_analyze(args: argparse.Namespace) -> int:
    db = _open_database(args)
    from hermes_checker.analysis import Analyzer
    from hermes_checker.collector.config import default_collector_config
    analyzer = Analyzer(db, default_collector_config())
    findings = analyzer.analyze_session(args.session)
    for f in findings:
        print(f"[{f.severity}] {f.finding_kind}  conf={f.confidence:.2f}\n  {f.message}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _tool_row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _load_pricing(args: argparse.Namespace) -> dict[str, Any]:
    if not getattr(args, "pricing_file", None):
        return {}
    from hermes_checker.accounting import load_pricing_profile
    table = load_pricing_profile(Path(args.pricing_file).expanduser())
    return {"default": table}


_HANDLERS = {
    "doctor": _cmd_doctor,
    "status": _cmd_status,
    "dashboard": _cmd_dashboard,
    "report": _cmd_report,
    "sessions": _cmd_sessions,
    "export": _cmd_export,
    "pricing": _cmd_pricing,
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
    "analyze": _cmd_analyze,
}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())