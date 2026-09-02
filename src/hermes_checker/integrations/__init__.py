"""Hermes plugin package — the user-plugin shim Hermes Agent loads.

This module lives in :mod:`hermes_checker.integrations.hermes_plugin` and
is shipped to the user's Hermes plugins directory by the
``hermes-checker install`` CLI subcommand.

The plugin keeps ZERO non-stdlib runtime dependencies so it can load
inside Hermes without polluting its environment.
"""
from .plugin import register

__all__ = ["register"]