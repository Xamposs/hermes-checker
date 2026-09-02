"""Prompt-component attribution.

Hermes does not expose per-section token counts (no system / tools /
memory / skills breakdown). Hermes Checker approximates this locally:

- We tokenize the JSON body of the messages Hermes is about to ship to
  the provider (received on ``pre_api_request`` and ``pre_llm_call``).
- We classify each message into a ``component`` by role and content
  heuristics.
- We sum character / token counts per component and persist the result
  alongside the corresponding :class:`api_requests` row.

The numbers are explicitly tagged ``LOCALLY_ESTIMATED`` in the database
and the report UI always shows ``Attribution error = local - provider``
so the user can see the unexplained delta.

What the labels mean
--------------------

- ``SYSTEM``               : role=system messages
- ``TOOLS_SCHEMA``         : role=system messages whose content looks like
                            a JSON tool/function schema (heuristic)
- ``SKILLS``               : role=system or user messages containing
                            ``[Loaded as part of the skill bundle`` or
                            similar markers we observed in the Hermes
                            source
- ``MEMORY``               : role=user messages containing explicit
                            "memory" / "recall" framing
- ``PROJECT_INSTRUCTIONS`` : role=system messages that begin with common
                            AGENTS.md / project-guidance markers
- ``USER_MESSAGES``        : role=user, not classified as memory / skills
- ``ASSISTANT_HISTORY``    : role=assistant messages
- ``TOOL_RESULTS``         : role=tool messages
- ``OTHER``                : everything we can't classify
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .tokenizer import Tokenizer, hash_text

COMPONENT_ORDER = (
    "SYSTEM",
    "TOOLS_SCHEMA",
    "SKILLS",
    "MEMORY",
    "PROJECT_INSTRUCTIONS",
    "USER_MESSAGES",
    "ASSISTANT_HISTORY",
    "TOOL_RESULTS",
    "OTHER",
)


@dataclass(frozen=True)
class ComponentAttribution:
    component: str
    characters: int
    bytes: int
    estimated_tokens: int
    measurement_method: str
    confidence: float
    source_identifier: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "characters": self.characters,
            "bytes": self.bytes,
            "estimated_tokens": self.estimated_tokens,
            "measurement_method": self.measurement_method,
            "confidence": self.confidence,
            "source_identifier": self.source_identifier,
        }


# Heuristics are intentionally conservative; we'd rather label
# something OTHER than mis-classify.
_SKILLS_MARKERS = (
    "skill bundle",
    "loaded as part of the `,ie=",
    "loaded as part of the skill bundle",
    "<skill>",
)
_MEMORY_MARKERS = (
    "## memory",
    "[memory]",
    "recalled context",
    "memory context:",
)
_PROJECT_MARKERS = (
    "agents.md",
    "## project instructions",
    "## repository conventions",
)


def classify_message_role(message: Mapping[str, Any]) -> str:
    """Classify one chat-completion message into a component bucket."""
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
        return "USER_MESSAGES"

    if role == "assistant":
        return "ASSISTANT_HISTORY"

    if role == "tool":
        return "TOOL_RESULTS"

    if role == "developer":
        # Some OpenAI-compatible servers use "developer" for system.
        return "SYSTEM"

    return "OTHER"


def attribute_messages(
    messages: Sequence[Mapping[str, Any]],
    tokenizer: Tokenizer,
) -> list[ComponentAttribution]:
    """Bucket every message into a component and return per-bucket attribution.

    The returned list has at most one entry per component. The bucket is
    summed across all messages in the input.
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
        component = classify_message_role(message)
        text = _content_to_text(message.get("content"))
        if not text:
            continue
        tc = tokenizer.count(text)
        bucket = accum[component]
        bucket["characters"] += tc.text_chars
        bucket["bytes"] += tc.text_bytes
        bucket["tokens"] += tc.tokens
        bucket["method"] = tc.method
        bucket["confidence_sum"] += confidence_for_role.get(role, 0.5)
        bucket["confidence_count"] += 1
        if bucket["source"] is None:
            bucket["source"] = f"{role}:{hash_text(text)[:12]}"

    out: list[ComponentAttribution] = []
    for name in COMPONENT_ORDER:
        b = accum[name]
        if b["characters"] == 0:
            continue
        conf = b["confidence_sum"] / b["confidence_count"] if b["confidence_count"] else 0.5
        out.append(
            ComponentAttribution(
                component=name,
                characters=b["characters"],
                bytes=b["bytes"],
                estimated_tokens=b["tokens"],
                measurement_method=b["method"],
                confidence=conf,
                source_identifier=b["source"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Heuristics (kept private)
# ---------------------------------------------------------------------------


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
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


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
    return any(marker in lower_text for marker in _PROJECT_MARKERS)


def _looks_like_skills(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _SKILLS_MARKERS)


def _looks_like_memory(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _MEMORY_MARKERS)


__all__ = [
    "ComponentAttribution",
    "classify_message_role",
    "attribute_messages",
    "COMPONENT_ORDER",
]