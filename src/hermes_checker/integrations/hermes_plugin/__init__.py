"""Hermes user-plugin entry point.

This file is what gets copied into ``~/.hermes/plugins/hermes-checker/``
and is loaded by ``hermes_cli.plugins.discover_and_load``.  It uses
ONLY stdlib + the already-importable :mod:`hermes_checker` package so
Hermes never has to install extra dependencies.

Hermes calls our ``register(ctx)`` function once, and we register
hooks for every event we care about.  The actual work happens in
:mod:`hermes_checker.collector.collector`, which is also importable by
the CLI / dashboard.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger("hermes_checker")

# Make sure the in-tree ``hermes_checker`` package is importable when
# Hermes loads us.  The plugin directory layout installed by
# ``hermes-checker install`` looks like::
#
#   ~/.hermes/plugins/hermes-checker/
#       plugin.yaml
#       __init__.py        # this file
#       hermes_checker/    # the full package, copied alongside
#
# ``__init__.py`` and the package directory sit next to each other, so
# we add this file's directory to ``sys.path`` and let normal package
# import resolution do the rest.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

try:
    from hermes_checker.collector import HookCollector  # type: ignore
    from hermes_checker.collector.config import (  # type: ignore
        CollectorConfig,
        default_collector_config,
    )
except Exception as exc:  # pragma: no cover - very defensive
    logger.warning("hermes_checker plugin failed to import core: %s", exc)
    HookCollector = None  # type: ignore


_collector: Any = None


def _get_collector() -> Any:
    """Lazily build a collector. Keeps cold-start cheap."""
    global _collector
    if _collector is not None:
        return _collector
    if HookCollector is None:
        return None
    config: CollectorConfig = default_collector_config()
    try:
        _collector = HookCollector(config=config)
    except Exception as exc:  # pragma: no cover - very defensive
        logger.warning("hermes_checker collector init failed: %s", exc)
        _collector = None
    return _collector


def register(ctx: Any) -> None:
    """Hook into every Hermes event we care about.

    ``ctx`` is a Hermes-provided PluginContext. We only call its
    ``register_hook(name, callback)`` method.
    """
    collector = _get_collector()
    if collector is None:
        return

    def safe(name: str):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return getattr(collector, name)(**kwargs)
            except Exception as exc:  # pragma: no cover
                logger.warning("hook %s failed: %s", name, exc)
                return None
        return wrapper

    # Session lifecycle
    ctx.register_hook("on_session_start", safe("on_session_start"))
    ctx.register_hook("on_session_end", safe("on_session_end"))
    ctx.register_hook("on_session_finalize", safe("on_session_finalize"))

    # LLM / API requests
    ctx.register_hook("pre_api_request", safe("pre_api_request"))
    ctx.register_hook("post_api_request", safe("post_api_request"))
    ctx.register_hook("api_request_error", safe("api_request_error"))
    # The pre/post_llm_call hooks carry per-turn context too — useful
    # as a backup correlation signal if the api_* hooks are unavailable.
    ctx.register_hook("pre_llm_call", safe("pre_llm_call"))
    ctx.register_hook("post_llm_call", safe("post_llm_call"))

    # Tool calls
    ctx.register_hook("pre_tool_call", safe("pre_tool_call"))
    ctx.register_hook("post_tool_call", safe("post_tool_call"))


__all__ = ["register"]