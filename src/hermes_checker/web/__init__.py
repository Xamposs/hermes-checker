"""Web dashboard package — FastAPI local dashboard.

The dashboard is intentionally tiny: a handful of JSON endpoints plus
four static HTML pages (LIVE / SESSION / ANALYTICS / INSIGHTS) served
from ``src/hermes_checker/web/static``.  No React, no build step.

The dashboard binds to ``127.0.0.1`` by default — never ``0.0.0.0``.
"""
from .app import build_app, run_dashboard

__all__ = ["build_app", "run_dashboard"]