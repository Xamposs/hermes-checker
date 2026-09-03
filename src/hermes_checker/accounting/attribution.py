"""Prompt-component attribution (V1.1, multi-section aware).

Priority chain (Issue 7):

1. Hermes-native prompt breakdown (``compute_prompt_breakdown``) — the
   gold standard when Hermes Agent is installed. Components are reported
   at the level Hermes itself uses (system / tools / skills / memory /
   user profile / etc.).
2. Hermes-native structured prompt parts (``build_system_prompt_parts``)
   when the system message exposes a sectioned shape.
3. A static snapshot of the request body and tool schemas when we have
   neither of the above (still labelled HERMES_NATIVE_ESTIMATE / LOCAL).
4. Section-aware parsing of the captured system message (regex over
   well-known section headings) — a final fallback.
5. The conservative role-based heuristic that classifies one
   ``role=system`` message as ``SYSTEM``.

We never claim exact per-component provider billing. Every bucket is
labelled with provenance so the dashboard / CLI can show:

    PROVIDER TOTAL = ground truth (Hermes's CanonicalUsage)
    LOCAL ATTRIBUTION = our best decomposition
    UNATTRIBUTED = provider - local (explicit gap)
    COVERAGE = local / provider (explicit percentage)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from hermes_checker import (
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_HERMES_NATIVE_ESTIMATE,
    PROVENANCE_HERMES_MEASURED,
)
from hermes_checker.accounting.tokenizer import Tokenizer, hash_text


# Order matters for the report UI. The first component is the largest
# "fixed" bucket on most workloads; we keep this stable across runs.
COMPONENT_ORDER = (
    "SYSTEM",
    "TOOLS_SCHEMA",
    "SKILLS",
    "MEMORY",
    "PROJECT_INSTRUCTIONS",
    "USER_PROFILE",
    "MCP_SCHEMAS",
    "SUBAGENT_DEFS",
    "USER_MESSAGES",
    "ASSISTANT_HISTORY",
    "TOOL_RESULTS",
    "OTHER",
)


@dataclass
class ComponentAttribution:
    """One component's contribution to a single request's prompt.

    All token counts are LOCALLY_ESTIMATED unless ``provenance`` is
    :data:`PROVENANCE_HERMES_MEASURED` / :data:`PROVENANCE_HERMES_NATIVE_ESTIMATE`.
    """

    component: str
    characters: int
    bytes: int
    estimated_tokens: int
    measurement_method: str
    confidence: float
    source_identifier: Optional[str] = None
    provenance: str = PROVENANCE_LOCALLY_ESTIMATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "characters": self.characters,
            "bytes": self.bytes,
            "estimated_tokens": self.estimated_tokens,
            "measurement_method": self.measurement_method,
            "confidence": self.confidence,
            "source_identifier": self.source_identifier,
            "provenance": self.provenance,
        }


# Headings we recognise inside a single ``role=system`` message. The
# detection is intentionally conservative: a heading is matched only
# when it appears at the start of a line, with optional leading
# whitespace. Hermes's prompts do include these markers (the spec
# mentioned "memory" and "skills index" in particular).
_SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    "TOOLS_SCHEMA": (
        "## Tool Definitions", "## Tools", "## Available Tools",
        "## Tool Schema", "## Tool Schemas",
        "## Function Definitions", "## Functions",
        "## Available Functions",
    ),
    "SKILLS": (
        "## Skills", "## Available Skills", "## Skills Index",
        "## <available_skills>", "<available_skills>",
        "## Skill Bundle", "## Loaded Skills",
        # Older Hermes builds tagged injected skill bodies with this
        # in-content marker.
        "[Loaded as part of the skill bundle:",
    ),
    "MEMORY": (
        "## Memory", "## Memories", "## Recall", "## Recalled Context",
        "## Recent Memories", "## Long-term Memory",
        # Older Hermes builds prefixed injected memory blocks with this
        # bare marker in user-message frames.
        "[memory]",
    ),
    "USER_PROFILE": (
        "## User Profile", "## Profile", "## User",
    ),
    "PROJECT_INSTRUCTIONS": (
        "## AGENTS.md", "## Project Instructions",
        "## Repository Conventions", "## Project Rules",
        "## Project Context",
    ),
    "MCP_SCHEMAS": (
        "## MCP Tools", "## MCP Schemas", "## MCP Server Tools",
    ),
    "SUBAGENT_DEFS": (
        "## Subagents", "## Subagent Definitions", "## Available Subagents",
    ),
}


# Section-aware attribution
# ---------------------------------------------------------------------------


def _split_system_sections(system_text: str) -> list[tuple[str, str]]:
    """Split a single ``role=system`` message into (component, text) pairs.

    The split is anchor-based: a heading marks the start of a new
    component. Anything before the first recognised heading is attributed
    to ``SYSTEM`` (identity / generic guidance).
    """
    if not system_text:
        return []
    lines = system_text.splitlines(keepends=True)
    splits: list[tuple[str, str]] = []
    current_comp = "SYSTEM"
    current_lines: list[str] = []
    headings: dict[str, tuple[str, ...]] = {k: v for k, v in _SECTION_HEADINGS.items()}
    flat: dict[str, str] = {}
    for comp, heads in headings.items():
        for h in heads:
            flat[h.lower()] = comp

    def _flush() -> None:
        if current_lines:
            splits.append((current_comp, "".join(current_lines)))

    for line in lines:
        stripped = line.strip().lower()
        # Match a line that starts with one of the headings.
        matched = None
        for h_lower, comp in flat.items():
            if stripped.startswith(h_lower):
                matched = comp
                break
        if matched is not None:
            _flush()
            current_comp = matched
            current_lines = [line]
        else:
            current_lines.append(line)
    _flush()
    return splits


def classify_message_role(message: Mapping[str, Any]) -> str:
    """Top-level role classification (unchanged from V1)."""
    role = (message.get("role") or "").lower()
    content = message.get("content")
    content_text = _content_to_text(content)
    content_lower = content_text.lower()

    if role == "system":
        if _looks_like_tools_schema(content_text):
            return "TOOLS_SCHEMA"
        if _looks_like_project_instructions(content_lower):
            return "PROJECT_INSTRUCTIONS"
        return "SYSTEM"

    if role == "user":
        if _looks_like_skills(content_lower):
            return "SKILLS"
        if _looks_like_memory(content_lower):
            return "MEMORY"
        if _looks_like_user_profile(content_lower):
            return "USER_PROFILE"
        if _looks_like_mcp(content_lower):
            return "MCP_SCHEMAS"
        if _looks_like_subagents(content_lower):
            return "SUBAGENT_DEFS"
        return "USER_MESSAGES"

    if role == "assistant":
        return "ASSISTANT_HISTORY"

    if role == "tool":
        return "TOOL_RESULTS"

    if role == "developer":
        return "SYSTEM"

    return "OTHER"


def attribute_messages(
    messages: Sequence[Mapping[str, Any]],
    tokenizer: Tokenizer,
    *,
    tools_schema_chars: int = 0,
) -> list[ComponentAttribution]:
    """Bucket every message into a component, returning per-bucket attribution.

    When a single ``role=system`` message contains recognisable sub-
    sections (Tools, Skills, Memory, AGENTS.md, etc.), we split the
    message by heading and attribute each piece separately.  This is the
    cheap fallback when Hermes-native ``compute_prompt_breakdown`` is
    not available.

    If ``tools_schema_chars`` is non-zero, we attribute that many chars
    to ``TOOLS_SCHEMA`` even if the system message did not include
    them (Hermes sometimes inlines the tool schemas via a separate code
    path we cannot see in the hook payload).
    """
    accum: dict[str, dict[str, Any]] = {
        name: {
            "characters": 0,
            "bytes": 0,
            "tokens": 0,
            "method": tokenizer.count("").method,
            "confidence_sum": 0.0,
            "confidence_count": 0,
            "source": None,
        }
        for name in COMPONENT_ORDER
    }

    confidence_for_role = {
        "system": 0.95,
        "user": 0.9,
        "assistant": 0.95,
        "tool": 0.95,
        "developer": 0.85,
        "unknown": 0.5,
    }

    for message in messages:
        role = (message.get("role") or "unknown").lower()
        text = _content_to_text(message.get("content"))
        if role == "system" and text:
            # Section-aware split. If the split leaves the entire
            # message as one ``SYSTEM`` chunk AND the message looks like
            # a tools schema / skills index / etc. (the cheap
            # content-shape check below), upgrade that chunk to the
            # matching component.
            for comp, section_text in _split_system_sections(text):
                if not section_text:
                    continue
                if comp == "SYSTEM":
                    comp = _upgrade_component_for_system_text(section_text)
                if not comp:
                    continue
                tc = tokenizer.count(section_text)
                _add_to_bucket(
                    accum[comp],
                    tc,
                    confidence_for_role.get(role, 0.5),
                    source=comp.lower(),
                )
            continue
        if not text:
            continue
        comp = classify_message_role(message)
        tc = tokenizer.count(text)
        _add_to_bucket(
            accum[comp],
            tc,
            confidence_for_role.get(role, 0.5),
            source=f"{role}:{hash_text(text)[:12]}",
        )

    # Optional: attribute the separately-emitted tool schemas
    if tools_schema_chars > 0:
        bucket = accum["TOOLS_SCHEMA"]
        bucket["characters"] += tools_schema_chars
        bucket["bytes"] += tools_schema_chars
        tc = tokenizer.count("x" * tools_schema_chars)
        bucket["tokens"] += tc.tokens
        bucket["confidence_count"] += 1
        bucket["confidence_sum"] += 0.7
        if not bucket["source"]:
            bucket["source"] = "tool_schemas_payload"

    out: list[ComponentAttribution] = []
    for name in COMPONENT_ORDER:
        b = accum[name]
        if b["characters"] == 0 and b["bytes"] == 0:
            continue
        conf = (b["confidence_sum"] / b["confidence_count"]
                if b["confidence_count"] else 0.5)
        out.append(ComponentAttribution(
            component=name,
            characters=int(b["characters"]),
            bytes=int(b["bytes"]),
            estimated_tokens=int(b["tokens"]),
            measurement_method=str(b["method"]),
            confidence=float(conf),
            source_identifier=b["source"],
            provenance=PROVENANCE_LOCALLY_ESTIMATED,
        ))
    return out


def attribution_coverage(
    components: Iterable[ComponentAttribution],
    *,
    provider_prompt_tokens: Optional[int],
) -> dict[str, Any]:
    """Return provider-vs-local coverage for one API request.

    Always returns the ``unattributed_tokens`` and ``coverage`` keys so
    the dashboard can show "we explained 73% of the provider total".
    """
    local_total = sum(c.estimated_tokens for c in components)
    out: dict[str, Any] = {
        "local_estimated_tokens": local_total,
        "provider_prompt_tokens": provider_prompt_tokens,
        "coverage": None,
        "unattributed_tokens": None,
    }
    if provider_prompt_tokens is None or provider_prompt_tokens <= 0:
        return out
    out["coverage"] = local_total / provider_prompt_tokens
    out["unattributed_tokens"] = provider_prompt_tokens - local_total
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_to_bucket(
    bucket: dict[str, Any],
    tc: Any,
    confidence: float,
    *,
    source: str,
) -> None:
    bucket["characters"] += tc.text_chars
    bucket["bytes"] += tc.text_bytes
    bucket["tokens"] += tc.tokens
    bucket["method"] = tc.method
    bucket["confidence_sum"] += confidence
    bucket["confidence_count"] += 1
    if not bucket["source"]:
        bucket["source"] = source


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif "text" in part and isinstance(part["text"], str):
                    parts.append(part["text"])
                else:
                    parts.append(_safe_json_dumps(part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    if isinstance(content, Mapping):
        return _safe_json_dumps(content)
    return str(content)


def _safe_json_dumps(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


# Heading patterns that fall back to the role-based heuristic
# (in case the section split didn't pick them up).
def _looks_like_tools_schema(text: str) -> bool:
    if not text:
        return False
    head = text[:4000].lstrip()
    if head.startswith("{") and ("\"tools\"" in head or "\"functions\"" in head):
        return True
    if "you have access to the following tools" in text.lower():
        return True
    if '"function"' in head and '"name"' in head and '"parameters"' in head:
        return True
    return False


def _looks_like_project_instructions(lower_text: str) -> bool:
    return any(marker in lower_text for marker in (
        "agents.md", "## project instructions", "## repository conventions",
        "## project context",
    ))


def _looks_like_skills(lower_text: str) -> bool:
    # Heading match
    if any(marker in lower_text for marker in _SECTION_HEADINGS["SKILLS"]):
        return True
    # Bare in-content marker used by older Hermes builds.
    return "[loaded as part of the skill bundle:" in lower_text


def _looks_like_memory(lower_text: str) -> bool:
    if any(marker in lower_text for marker in _SECTION_HEADINGS["MEMORY"]):
        return True
    # Bare in-content marker used by older Hermes builds.
    return lower_text.lstrip().startswith("[memory]") or " [memory]" in lower_text


def _looks_like_user_profile(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _SECTION_HEADINGS["USER_PROFILE"])


def _looks_like_mcp(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _SECTION_HEADINGS["MCP_SCHEMAS"])


def _looks_like_subagents(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _SECTION_HEADINGS["SUBAGENT_DEFS"])


def _upgrade_component_for_system_text(text: str) -> Optional[str]:
    """If a single-system-message chunk has no heading, infer its component.

    Hermes does not always prefix the inline tool-schema blob with
    ``## Tools``. When the section split leaves the whole message in
    one ``SYSTEM`` chunk, we use the cheap content-shape checks to
    upgrade it to ``TOOLS_SCHEMA`` / ``SKILLS`` / ``MEMORY`` / etc.
    """
    if not text:
        return "SYSTEM"
    lower = text.lower()
    if _looks_like_tools_schema(text):
        return "TOOLS_SCHEMA"
    if _looks_like_skills(lower):
        return "SKILLS"
    if _looks_like_memory(lower):
        return "MEMORY"
    if _looks_like_user_profile(lower):
        return "USER_PROFILE"
    if _looks_like_mcp(lower):
        return "MCP_SCHEMAS"
    if _looks_like_subagents(lower):
        return "SUBAGENT_DEFS"
    if _looks_like_project_instructions(lower):
        return "PROJECT_INSTRUCTIONS"
    return "SYSTEM"


__all__ = [
    "ComponentAttribution",
    "classify_message_role",
    "attribute_messages",
    "attribution_coverage",
    "COMPONENT_ORDER",
]