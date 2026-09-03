"""Hermes Checker — local, non-invasive observability for Hermes Agent / Hermes Desktop.

The package is organised by responsibility:

    storage       — SQLite persistence + schema migrations
    accounting    — token math, attribution, pricing, sanitization
    collector     — turns Hermes hook payloads into our normalised records
    integrations
      hermes_plugin — the user plugin that registers Hermes hook callbacks
    analysis      — rule-based waste / insight detection
    web           — FastAPI local dashboard (optional dependency)
    cli           — `hermes-checker` command-line entry point

Everything is designed so the package can also be imported directly by the
Hermes user-plugin shim (which has no Python dependency budget) — the
in-tree plugin only depends on :mod:`hermes_checker.collector` and the
stdlib.
"""
from __future__ import annotations

__all__ = ["__version__", "PROVENANCE"]

__version__ = "0.2.0"

# Provenance categories — every metric in Hermes Checker is tagged with one
# of these so the UI/CLI can show whether a number came from the provider
# (most authoritative), Hermes itself (authoritative for internal state),
# was calculated locally from captured data, or estimated because the
# provider/Hermes didn't expose the relevant field.
#
# Six labels:
#   PROVIDER_MEASURED       - read directly from the provider's response
#   HERMES_MEASURED         - read from a Hermes runtime field (counts, etc.)
#   HERMES_NATIVE_ESTIMATE  - computed by Hermes's own offline breakdown code
#                             (e.g. prompt_size.compute_prompt_breakdown)
#   LOCALLY_CALCULATED      - derived locally from measured inputs
#   LOCALLY_ESTIMATED       - tokenised/estimated locally (best-effort)
#   UNAVAILABLE             - not provided by the source for this record
PROVENANCE_PROVIDER_MEASURED = "PROVIDER_MEASURED"
PROVENANCE_HERMES_MEASURED = "HERMES_MEASURED"
PROVENANCE_HERMES_NATIVE_ESTIMATE = "HERMES_NATIVE_ESTIMATE"
PROVENANCE_LOCALLY_CALCULATED = "LOCALLY_CALCULATED"
PROVENANCE_LOCALLY_ESTIMATED = "LOCALLY_ESTIMATED"
PROVENANCE_UNAVAILABLE = "UNAVAILABLE"

PROVENANCE = frozenset({
    PROVENANCE_PROVIDER_MEASURED,
    PROVENANCE_HERMES_MEASURED,
    PROVENANCE_HERMES_NATIVE_ESTIMATE,
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_UNAVAILABLE,
})