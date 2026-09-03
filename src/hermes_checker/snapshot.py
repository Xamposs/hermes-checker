"""Static prompt snapshot — captures the FIXED prompt overhead once.

The ``hermes-checker snapshot`` command (Issue 6) writes one row to
``static_prompt_snapshots`` plus per-skill and per-toolset rows. We
prefer Hermes-native ``compute_prompt_breakdown`` and fall back to a
local tokenizer pass when Hermes is not importable.

The snapshot is NOT executed on every API hook — it runs once per
operator invocation. The collector records the latest snapshot id on
every ``api_requests`` row so the dashboard can join request-level
attribution with the static baseline.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from hermes_checker import (
    PROVENANCE_HERMES_MEASURED,
    PROVENANCE_HERMES_NATIVE_ESTIMATE,
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_UNAVAILABLE,
)
from hermes_checker.hermes_native import (
    NativeBreakdown,
    get_native_bridge,
)
from hermes_checker.storage import Database, DatabasePaths

logger = logging.getLogger("hermes_checker.snapshot")


def _breakdown_to_db_row(b: NativeBreakdown) -> dict[str, Any]:
    """Translate a NativeBreakdown into the static_prompt_snapshots row dict."""
    stable = b.tiers.get("stable", {})
    context = b.tiers.get("context", {})
    volatile = b.tiers.get("volatile", {})

    def _sum(*dicts) -> dict[str, int]:
        return {
            "chars": sum(int(d.get("chars", 0) or 0) for d in dicts),
            "bytes": sum(int(d.get("bytes", 0) or 0) for d in dicts),
            "tokens_est": sum(int(d.get("tokens_est", 0) or 0) for d in dicts),
        }

    system = _sum(stable, context, volatile)
    return {
        "taken_at": b.taken_at,
        "hermes_version": b.hermes_version,
        "platform": b.platform,
        "model": b.model,
        "base_url": b.base_url,
        "system_prompt_chars": system["chars"],
        "system_prompt_bytes": system["bytes"],
        "system_prompt_tokens_est": system["tokens_est"],
        "stable_chars": int(stable.get("chars", 0) or 0),
        "stable_bytes": int(stable.get("bytes", 0) or 0),
        "stable_tokens_est": int(stable.get("tokens_est", 0) or 0),
        "context_chars": int(context.get("chars", 0) or 0),
        "context_bytes": int(context.get("bytes", 0) or 0),
        "context_tokens_est": int(context.get("tokens_est", 0) or 0),
        "volatile_chars": int(volatile.get("chars", 0) or 0),
        "volatile_bytes": int(volatile.get("bytes", 0) or 0),
        "volatile_tokens_est": int(volatile.get("tokens_est", 0) or 0),
        "skills_index_chars": int(b.skills_index.get("chars", 0) or 0),
        "skills_index_bytes": int(b.skills_index.get("bytes", 0) or 0),
        "skills_index_tokens_est": int(b.skills_index.get("tokens_est", 0) or 0),
        "memory_chars": int(b.memory.get("chars", 0) or 0),
        "memory_bytes": int(b.memory.get("bytes", 0) or 0),
        "memory_tokens_est": int(b.memory.get("tokens_est", 0) or 0),
        "user_profile_chars": int(b.user_profile.get("chars", 0) or 0),
        "user_profile_bytes": int(b.user_profile.get("bytes", 0) or 0),
        "user_profile_tokens_est": int(b.user_profile.get("tokens_est", 0) or 0),
        "tools_count": int(b.tools.get("count", 0) or 0),
        "tools_json_bytes": int(b.tools.get("json_bytes", 0) or 0),
        "tools_json_tokens_est": int(b.tools.get("json_tokens_est", 0) or 0),
        "mcp_schemas_chars": int(b.mcp_schemas.get("chars", 0) or 0),
        "mcp_schemas_bytes": int(b.mcp_schemas.get("bytes", 0) or 0),
        "mcp_schemas_tokens_est": int(b.mcp_schemas.get("tokens_est", 0) or 0),
        "subagent_defs_chars": int(b.subagent_defs.get("chars", 0) or 0),
        "subagent_defs_bytes": int(b.subagent_defs.get("bytes", 0) or 0),
        "subagent_defs_tokens_est": int(b.subagent_defs.get("tokens_est", 0) or 0),
        "other_chars": int(b.other.get("chars", 0) or 0),
        "other_bytes": int(b.other.get("bytes", 0) or 0),
        "other_tokens_est": int(b.other.get("tokens_est", 0) or 0),
        "tokenizer_method": b.tokenizer_method,
        "hermes_native": int(b.hermes_native),
        "metadata_json": json.dumps({
            "reason": b.reason,
            "provenance": b.provenance,
            **b.metadata,
        }),
    }


def _breakdown_to_skill_rows(b: NativeBreakdown) -> list[dict[str, Any]]:
    rows = []
    for idx, s in enumerate(b.skills_breakdown or []):
        rows.append({
            "skill_name": str(s.get("name") or s.get("skill_name") or ""),
            "index_line_chars": int(s.get("index_line_chars", 0) or 0),
            "index_line_bytes": int(s.get("index_line_bytes", 0) or 0),
            "index_line_tokens_est": int(s.get("index_line_tokens_est", 0) or 0),
            "skill_md_chars": int(s.get("skill_md_chars", 0) or 0),
            "skill_md_bytes": int(s.get("skill_md_bytes", 0) or 0),
            "skill_md_tokens_est": int(s.get("skill_md_tokens_est", 0) or 0),
            "rank_in_index": idx,
        })
    return rows


def _breakdown_to_toolset_rows(b: NativeBreakdown) -> list[dict[str, Any]]:
    rows = []
    for t in b.toolsets_breakdown or []:
        # Hermes returns ``toolset`` (the key name) and ``json_bytes`` for
        # the schema payload size; we also accept ``name``/``schema_bytes``
        # in case the schema is extended in the future.
        name = str(t.get("toolset") or t.get("name") or t.get("toolset_name") or "?")
        schema_bytes = int(
            t.get("json_bytes", 0) or t.get("schema_bytes", 0) or 0
        )
        rows.append({
            "toolset_name": name,
            "tool_count": int(t.get("tool_count", 0) or 0),
            "schema_chars": int(t.get("schema_chars", 0) or 0),
            "schema_bytes": schema_bytes,
            "schema_tokens_est": int(t.get("schema_tokens_est", 0) or 0),
        })
    return rows


def render_breakdown_text(b: NativeBreakdown) -> str:
    """Render a NativeBreakdown as console-friendly text."""
    lines: list[str] = []
    lines.append("HERMES CHECKER — STATIC PROMPT SNAPSHOT")
    lines.append("=" * 60)
    lines.append(
        "taken_at:        "
        + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(b.taken_at))
    )
    lines.append(f"platform:        {b.platform or '?'}")
    lines.append(f"model:           {b.model or '?'}")
    if b.hermes_version:
        lines.append(f"hermes version:  {b.hermes_version[:12]}")
    lines.append(f"provenance:      {b.provenance}")
    lines.append(f"hermes_native:   {b.hermes_native}")
    if b.reason:
        lines.append(f"reason:          {b.reason}")
    lines.append("")

    # Tier breakdown
    lines.append("TIERS")
    lines.append("-" * 60)
    for name, t in b.tiers.items():
        lines.append(
            f"  {name:<10} chars={t.get('chars', 0):>8,}  "
            f"bytes={t.get('bytes', 0):>8,}  "
            f"tokens_est={t.get('tokens_est', 0):>7,}"
        )
    lines.append("")

    # Component attribution
    lines.append("COMPONENTS")
    lines.append("-" * 60)
    def _sum(*dicts) -> dict[str, int]:
        return {
            "chars": sum(int(d.get("chars", 0) or 0) for d in dicts),
            "bytes": sum(int(d.get("bytes", 0) or 0) for d in dicts),
            "tokens_est": sum(int(d.get("tokens_est", 0) or 0) for d in dicts),
        }
    components = [
        ("system (sum)", _sum(b.tiers.get("stable", {}),
                               b.tiers.get("context", {}),
                               b.tiers.get("volatile", {}))),
        # Tools: Hermes reports ``count`` and ``json_bytes`` (the compact
        # schema payload size).  We surface a synthetic ``chars`` field so
        # the table below can render both axes uniformly.
        ("tools schemas", {
            "chars": b.tools.get("json_bytes", 0) or 0,
            "bytes": b.tools.get("json_bytes", 0) or 0,
            "tokens_est": b.tools.get("json_tokens_est", 0) or 0,
        }),
        ("skills index", b.skills_index),
        ("memory", b.memory),
        ("user profile", b.user_profile),
        ("MCP schemas", b.mcp_schemas),
        ("subagent defs", b.subagent_defs),
        ("other", b.other),
    ]
    for name, d in components:
        if not d:
            continue
        tokens = d.get("tokens_est", 0) or 0
        chars = d.get("chars", 0) or 0
        lines.append(
            f"  {name:<16} chars={chars:>8,}  tokens_est={tokens:>7,}"
        )
    if b.tools.get("count"):
        lines.append(f"  tools count:    {b.tools['count']}")

    if b.skills_breakdown:
        lines.append("")
        lines.append("PER-SKILL (largest first)")
        lines.append("-" * 60)
        for s in b.skills_breakdown[:10]:
            name = s.get("name") or s.get("skill_name") or "?"
            idx_b = int(s.get("index_line_bytes", 0) or 0)
            sm_b = int(s.get("skill_md_bytes", 0) or 0)
            idx_t = int(s.get("index_line_tokens_est", 0) or 0)
            sm_t = int(s.get("skill_md_tokens_est", 0) or 0)
            lines.append(
                f"  {name:<32}  index={idx_b:>6,}B ({idx_t:>5,}t)  "
                f"SKILL.md={sm_b:>7,}B ({sm_t:>5,}t)"
            )
        if len(b.skills_breakdown) > 10:
            lines.append(f"  ... and {len(b.skills_breakdown) - 10} more")

    if b.toolsets_breakdown:
        lines.append("")
        lines.append("PER-TOOLSET")
        lines.append("-" * 60)
        for t in b.toolsets_breakdown:
            name = t.get("toolset") or t.get("name") or t.get("toolset_name") or "?"
            count = t.get("tool_count", 0)
            schema_t = t.get("schema_tokens_est", 0) or 0
            lines.append(
                f"  {name:<28}  tools={count:>3}  schema={schema_t:>6,}t"
            )

    lines.append("")
    return "\n".join(lines)


def run_snapshot(
    *,
    platform: str = "cli",
    paths: Optional[DatabasePaths] = None,
    hermes_home: Optional[Any] = None,
) -> tuple[int, NativeBreakdown]:
    """Compute a snapshot, persist it, and return the new id + breakdown."""
    paths = paths or DatabasePaths.default()
    bridge = get_native_bridge(hermes_home)
    breakdown = bridge.compute(platform=platform)
    breakdown.taken_at = time.time()

    db = Database(paths)
    try:
        sid = db.insert_static_snapshot(
            _breakdown_to_db_row(breakdown),
            skills=_breakdown_to_skill_rows(breakdown),
            toolsets=_breakdown_to_toolset_rows(breakdown),
        )
    finally:
        db.close()
    return sid, breakdown


__all__ = [
    "run_snapshot",
    "render_breakdown_text",
]