"""Lightweight tokenizer adapter.

Hermes Checker never tries to be the source of truth on token counts —
those come from the provider (or from Hermes's own ``CanonicalUsage``
normalisation). This module exists only to support the LOCAL component
attribution pass, where we tokenize the messages Hermes is about to ship
to estimate how many tokens each section costs.

We:

1. Prefer :mod:`tiktoken` when installed (encoding chosen based on the
   model family if known; otherwise ``cl100k_base``).
2. Fall back to a heuristic ``chars / 4`` estimator. The fallback is
   explicitly labelled ``ESTIMATED`` everywhere we use it.

Nothing here ever downloads model weights or large artifacts; if
tiktoken isn't installed and the user wants accurate tokenization,
``pip install hermes-checker[tokenizers]`` is the opt-in path.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional

from hermes_checker import (
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_PROVIDER_MEASURED,
)

try:  # pragma: no cover - exercised only when the optional dep is present
    import tiktoken  # type: ignore[import-not-found]

    _HAS_TIKTOKEN = True
except Exception:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]
    _HAS_TIKTOKEN = False


# Encoding hints keyed off well-known model families.
_ENCODING_HINTS: dict[str, str] = {
    "gpt-4": "cl100k_base",
    "gpt-4o": "o200k_base",
    "gpt-5": "o200k_base",
    "claude": "cl100k_base",
    "deepseek": "cl100k_base",
    "minimax": "cl100k_base",
    "qwen": "cl100k_base",
    "gemini": "cl100k_base",
    "llama": "cl100k_base",
    "grok": "cl100k_base",
}

_DEFAULT_ENCODING = "cl100k_base"


@dataclass(frozen=True)
class TokenCount:
    """Result of tokenizing a piece of text.

    ``method`` is one of:
    - ``"TIKTOKEN"`` when tiktoken produced the count exactly
    - ``"HEURISTIC"`` when we used the chars/4 fallback
    """

    text_chars: int
    text_bytes: int
    tokens: int
    method: str
    encoding: str

    @property
    def provenance(self) -> str:
        if self.method == "TIKTOKEN":
            # tiktoken is still a local count, not provider-measured
            return PROVENANCE_LOCALLY_ESTIMATED
        return PROVENANCE_LOCALLY_ESTIMATED


def _encoding_for_model(model: Optional[str]) -> str:
    if not model:
        return _DEFAULT_ENCODING
    m = model.lower()
    for needle, enc in _ENCODING_HINTS.items():
        if needle in m:
            return enc
    return _DEFAULT_ENCODING


class Tokenizer:
    """A thin wrapper that picks the best available counter.

    Instances are cheap; creating one per call is fine.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model
        self.encoding_name = _encoding_for_model(model)
        self._encoder = None
        if _HAS_TIKTOKEN:
            try:
                self._encoder = tiktoken.get_encoding(self.encoding_name)
            except Exception:
                self._encoder = None

    @property
    def is_exact(self) -> bool:
        return self._encoder is not None

    def count(self, text: Optional[str]) -> TokenCount:
        if text is None:
            return TokenCount(0, 0, 0, "HEURISTIC", self.encoding_name)
        chars = len(text)
        byte_len = len(text.encode("utf-8", errors="replace"))
        if self._encoder is not None:
            try:
                tokens = len(self._encoder.encode(text))
                return TokenCount(chars, byte_len, tokens, "TIKTOKEN", self.encoding_name)
            except Exception:
                pass
        # Heuristic fallback: ~4 chars per token for English-like text.
        # We round up so we never under-count a small string.
        est = max(1, (chars + 3) // 4) if chars else 0
        return TokenCount(chars, byte_len, est, "HEURISTIC", self.encoding_name)

    def count_many(self, texts: Iterable[Optional[str]]) -> TokenCount:
        total_chars = 0
        total_bytes = 0
        if self._encoder is not None:
            total_tokens = 0
            try:
                for t in texts:
                    if not t:
                        continue
                    total_chars += len(t)
                    total_bytes += len(t.encode("utf-8", errors="replace"))
                    total_tokens += len(self._encoder.encode(t))
                return TokenCount(
                    total_chars, total_bytes, total_tokens, "TIKTOKEN", self.encoding_name
                )
            except Exception:
                pass
        for t in texts:
            if not t:
                continue
            total_chars += len(t)
            total_bytes += len(t.encode("utf-8", errors="replace"))
        tokens = max(1, (total_chars + 3) // 4) if total_chars else 0
        return TokenCount(
            total_chars, total_bytes, tokens, "HEURISTIC", self.encoding_name
        )


_default_tokenizer: Optional[Tokenizer] = None


def get_tokenizer(model: Optional[str] = None) -> Tokenizer:
    """Return a tokenizer (cached when no model is specified)."""
    global _default_tokenizer
    if model is None:
        if _default_tokenizer is None:
            _default_tokenizer = Tokenizer()
        return _default_tokenizer
    return Tokenizer(model)


def hash_text(text: Optional[str]) -> str:
    """SHA256 of text bytes (defence-in-depth fingerprinting)."""
    if text is None:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def hash_bytes(data: Optional[bytes]) -> str:
    if data is None:
        return ""
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "Tokenizer",
    "get_tokenizer",
    "TokenCount",
    "hash_text",
    "hash_bytes",
]