"""Sanitization helpers — defence-in-depth secret handling.

Hermes already redacts the primary secret keys (``api_key``,
``authorization``, ``proxy_authorization``, ``cookie``, ``set_cookie``,
``*_api_key``) before handing payload data to plugins. We run a SECOND
pass on every value we persist, both because Hermes's policy may evolve
and because Hermes Checker's own code receives text from tools,
providers, and other plugins that were not pre-sanitised by Hermes.

What we do
----------

- :func:`sanitize_text` scans a string for secret-shaped patterns
  (API keys, bearer tokens, JWT, OpenAI/Anthropic/OpenRouter-style
  strings, AWS keys, generic high-entropy ``sk-...`` / ``pk-...``
  prefixes) and replaces them with ``<redacted>``.
- :func:`sanitize_dict` walks any mapping/list structure and applies
  :func:`sanitize_text` to string leaves while preserving the shape.
- :func:`truncate_for_storage` bounds a string's size before we write
  it to disk.

These helpers are intentionally aggressive; we err on the side of
redaction because V1 is metadata-only storage.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# Maximum length we will write for any single text column.  Tuned to
# keep the DB small while still being long enough for any reasonable
# args summary.
DEFAULT_MAX_CHARS = 8000

# Patterns are compiled once.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    # OpenAI / OpenRouter style
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"pk-[A-Za-z0-9_\-]{20,}"),
    # JWT
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # AWS access key
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # GitHub PAT
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # Slack tokens
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    # Authorization / Bearer header values
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:Bearer\s+)?[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    # Generic api_key=value style
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)(?:['\"]?)[A-Za-z0-9._\-]{12,}(?:['\"]?)"),
)

_REDACTED = "<redacted>"


def sanitize_text(text: str | None, *, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    """Return a copy of *text* with secret patterns redacted and length bounded.

    ``None`` in → ``None`` out. Strings shorter than the cap are
    returned unchanged after the regex pass.
    """
    if text is None:
        return None
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_REDACTED, out)
    if len(out) > max_chars:
        out = out[:max_chars] + f"...[truncated {len(out) - max_chars} chars]"
    return out


def sanitize_dict(value: Any, *, max_chars: int = DEFAULT_MAX_CHARS,
                  _depth: int = 0, _max_depth: int = 6) -> Any:
    """Recursively sanitize strings inside a JSON-like structure."""
    if _depth > _max_depth:
        return "<depth limit>"
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_text(value, max_chars=max_chars)
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            (sanitize_text(str(k), max_chars=200) if isinstance(k, str) else str(k)):
            sanitize_dict(v, max_chars=max_chars, _depth=_depth + 1, _max_depth=_max_depth)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        cleaned = [
            sanitize_dict(v, max_chars=max_chars, _depth=_depth + 1, _max_depth=_max_depth)
            for v in seq[:200]
        ]
        if len(seq) > 200:
            cleaned.append({"_truncated_items": len(seq) - 200})
        return cleaned
    return sanitize_text(str(value), max_chars=max_chars)


def truncate_for_storage(text: str | None, *, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    """Bound a string's length without touching secrets (use :func:`sanitize_text` first)."""
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[truncated {len(text) - max_chars} chars]"


__all__ = [
    "sanitize_text",
    "sanitize_dict",
    "truncate_for_storage",
    "DEFAULT_MAX_CHARS",
]