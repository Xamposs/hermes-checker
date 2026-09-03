"""Hermes Checker integration package.

The actual user plugin that Hermes Agent loads lives in
:mod:`hermes_checker.integrations.hermes_plugin` — that is what gets
copied into ``~/.hermes/plugins/hermes-checker/`` by the
``hermes-checker install`` CLI subcommand.

This top-level package is here so we can import integration helpers
from the Hermes Checker Python package (e.g. for the dashboard).
"""
