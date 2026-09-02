"""Analysis package — rule-based waste / insight detection.

The analyzer runs LOCALLY on the data already captured in the database.
It does NOT call external services or LLMs and it does NOT modify
Hermes's behaviour.  Every finding is written to ``optimizer_findings``
with a confidence score and a structured ``evidence_json`` blob so the
UI/CLI can show the user exactly what triggered it.
"""
from .analyzer import Analyzer, Finding, POTENTIAL_WASTE, HIGH_OVERHEAD, REPEATED_CONTENT, OBSERVATION

__all__ = [
    "Analyzer",
    "Finding",
    "POTENTIAL_WASTE",
    "HIGH_OVERHEAD",
    "REPEATED_CONTENT",
    "OBSERVATION",
]