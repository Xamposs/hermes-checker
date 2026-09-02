"""Pricing engine.

Pricing profiles are YAML files (see ``config/pricing.example.yaml``).
A profile is a mapping of ``model`` → :class:`PricingEntry`.  Cost is
computed as::

    cost = (input_tokens / 1M) * input_per_million_usd
         + (cached_input / 1M) * cached_input_per_million_usd
         + (cache_write / 1M) * cache_write_per_million_usd
         + (output_tokens / 1M) * output_per_million_usd
         + (reasoning_tokens / 1M) * reasoning_per_million_usd
         + request_count * request_cost_usd

Cached and uncached buckets are billed separately when both rates are
known.  When a profile is missing a field we silently treat that field
as zero (cost-omitting) — never fabricate a price.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


_PRIMITIVE = (str, int, float, bool)


@dataclass(frozen=True)
class PricingEntry:
    model: str
    input_per_million_usd: float = 0.0
    cached_input_per_million_usd: Optional[float] = None
    cache_write_per_million_usd: Optional[float] = None
    output_per_million_usd: float = 0.0
    reasoning_per_million_usd: Optional[float] = None
    request_cost_usd: float = 0.0
    notes: str = ""

    def effective_cached_input_rate(self) -> float:
        # When the user only gave an input rate, use it (no cache discount).
        # When only a cached rate is given, treat uncached as the cached
        # rate too — explicitly opt-in via notes.
        if self.cached_input_per_million_usd is not None:
            return float(self.cached_input_per_million_usd)
        return float(self.input_per_million_usd)


@dataclass
class PricingTable:
    """In-memory table of pricing profiles.

    The shape on disk is::

        profiles:
          - name: openrouter-2026-09
            models:
              anthropic/claude-4-sonnet:
                input_per_million_usd: 3.0
                cached_input_per_million_usd: 0.30
                output_per_million_usd: 15.0
                reasoning_per_million_usd: 15.0
              ...

    ``name`` is the profile label; we expose ``table[profile_name]`` to
    fetch a :class:`PricingTable` and ``table[profile_name][model]`` to
    fetch a :class:`PricingEntry`.
    """

    profiles: dict[str, "Profile"] = field(default_factory=dict)

    def add(self, profile_name: str, entry: PricingEntry) -> None:
        self.profiles.setdefault(profile_name, Profile(name=profile_name))
        self.profiles[profile_name].models[entry.model] = entry

    def get(self, profile_name: str) -> Optional["Profile"]:
        return self.profiles.get(profile_name)

    def profile_names(self) -> list[str]:
        return sorted(self.profiles.keys())


@dataclass
class Profile:
    name: str
    models: dict[str, PricingEntry] = field(default_factory=dict)

    def lookup(self, model: str) -> Optional[PricingEntry]:
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        # Try suffix/prefix match so "claude-4-sonnet" still finds
        # "anthropic/claude-4-sonnet" when the request log omits the
        # vendor prefix.
        for known, entry in self.models.items():
            if model.endswith(known) or known.endswith(model):
                return entry
        return None

    def __getitem__(self, model: str) -> PricingEntry:
        entry = self.lookup(model)
        if entry is None:
            raise KeyError(model)
        return entry


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _to_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_pricing_profile(path: Path) -> PricingTable:
    """Load a single pricing YAML file.

    We prefer PyYAML when it is installed (it handles real-world YAML
    correctly); otherwise we fall back to a tiny inline parser that
    handles the regular subset our pricing files use.
    """
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
        data = yaml.safe_load(text) or {}
    except Exception:
        data = _parse_simple_yaml(text)

    table = PricingTable()

    # Accept either a bare {"models": {...}} or {"profiles": [{"name": ..., "models": {...}}]}.
    if isinstance(data, dict) and "models" in data and isinstance(data["models"], dict):
        # Treat as a single profile, named after the file stem.
        profile = Profile(name=path.stem)
        for model, fields in data["models"].items():
            entry = _pricing_entry_from_dict(str(model), fields, notes=str(data.get("notes", "")))
            profile.models[entry.model] = entry
        table.profiles[profile.name] = profile
        return table

    if isinstance(data, dict) and "profiles" in data:
        for prof in data["profiles"]:
            if not isinstance(prof, dict):
                continue
            name = str(prof.get("name") or path.stem)
            profile = Profile(name=name)
            notes_default = str(prof.get("notes", ""))
            for model, fields in (prof.get("models") or {}).items():
                entry = _pricing_entry_from_dict(str(model), fields, notes=notes_default)
                profile.models[entry.model] = entry
            table.profiles[profile.name] = profile
    return table


def _pricing_entry_from_dict(model: str, fields: Any, notes: str) -> PricingEntry:
    if not isinstance(fields, dict):
        fields = {}
    return PricingEntry(
        model=model,
        input_per_million_usd=_to_float(fields.get("input_per_million_usd")),
        cached_input_per_million_usd=_to_optional_float(fields.get("cached_input_per_million_usd")),
        cache_write_per_million_usd=_to_optional_float(fields.get("cache_write_per_million_usd")),
        output_per_million_usd=_to_float(fields.get("output_per_million_usd")),
        reasoning_per_million_usd=_to_optional_float(fields.get("reasoning_per_million_usd")),
        request_cost_usd=_to_float(fields.get("request_cost_usd")),
        notes=str(fields.get("notes", notes)),
    )


def compute_cost(
    entry: Optional[PricingEntry],
    *,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    request_count: int = 1,
) -> Optional[float]:
    """Return USD cost using the given entry, or None if entry is missing."""
    if entry is None:
        return None
    total = 0.0
    if input_tokens:
        total += (input_tokens / 1_000_000.0) * entry.input_per_million_usd
    if cache_read_tokens:
        rate = entry.effective_cached_input_rate()
        total += (cache_read_tokens / 1_000_000.0) * rate
    if cache_write_tokens and entry.cache_write_per_million_usd is not None:
        total += (cache_write_tokens / 1_000_000.0) * entry.cache_write_per_million_usd
    if output_tokens:
        total += (output_tokens / 1_000_000.0) * entry.output_per_million_usd
    if reasoning_tokens and entry.reasoning_per_million_usd is not None:
        total += (reasoning_tokens / 1_000_000.0) * entry.reasoning_per_million_usd
    if request_count:
        total += request_count * entry.request_cost_usd
    return round(total, 6)


# ---------------------------------------------------------------------------
# Tiny YAML parser — only the subset our pricing files need.
# ---------------------------------------------------------------------------


_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<rest>.*)$")
_KEY_VALUE_RE = re.compile(r"^(?P<key>[A-Za-z0-9_./\-]+)\s*:\s*(?P<value>.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*-\s*(?P<value>.*)$")


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a tiny YAML subset:

    - ``key: value``
    - ``key:``  (mapping value follows indented)
    - ``- item`` (list item; nested mappings indented under it)
    - bare scalars (``true``/``false``/``null``/numbers)

    Sufficient for our pricing YAML files.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        lines.append((indent, raw.strip()))

    root: dict[str, Any] = {}

    # Iterative descent. At each step we know the current container
    # (dict or list) and the indent of the next line.
    pos = 0

    def parse_block(container: Any, container_indent: int) -> None:
        nonlocal pos
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < container_indent or not content:
                return  # block ended
            if indent > container_indent:
                # Indented line we don't own — caller will close us out.
                return
            # Same indent as our container.
            if isinstance(container, list):
                # We expect "- ..." or a continuation of the previous item.
                if content.startswith("- "):
                    item_content = content[2:].lstrip()
                    pos += 1
                    if not item_content:
                        # Empty "-": the list item is a block starting
                        # at the next deeper indent.
                        # Peek at the next line to pick dict vs list.
                        child_indent = container_indent + 2
                        if pos < len(lines) and lines[pos][0] >= child_indent:
                            kind = _peek_kind(lines, pos, child_indent)
                            child: Any = [] if kind == "list" else {}
                            container.append(child)
                            parse_block(child, child_indent)
                        else:
                            container.append(None)
                        continue
                    kv = _KEY_VALUE_RE.match(item_content)
                    if kv:
                        key = kv.group("key")
                        value = kv.group("value").strip()
                        item: dict[str, Any] = {}
                        container.append(item)
                        if value:
                            item[key] = _parse_scalar(value)
                        else:
                            child_indent = container_indent + 4
                            if pos < len(lines) and lines[pos][0] >= child_indent:
                                kind = _peek_kind(lines, pos, child_indent)
                                sub: Any = [] if kind == "list" else {}
                                item[key] = sub
                                parse_block(sub, child_indent)
                    else:
                        container.append(_parse_scalar(item_content))
                    continue
                # Continuation of previous list item mapping (more keys)
                kv = _KEY_VALUE_RE.match(content)
                if kv and isinstance(container[-1], dict):
                    key = kv.group("key")
                    value = kv.group("value").strip()
                    if value:
                        container[-1][key] = _parse_scalar(value)
                    else:
                        child_indent = container_indent + 2
                        kind = _peek_kind(lines, pos + 1, child_indent)
                        sub = [] if kind == "list" else {}
                        container[-1][key] = sub
                        pos += 1
                        parse_block(sub, child_indent)
                        continue
                pos += 1
                continue

            # container is a dict.
            kv = _KEY_VALUE_RE.match(content)
            if kv:
                key = kv.group("key")
                value = kv.group("value").strip()
                pos += 1
                if value:
                    container[key] = _parse_scalar(value)
                else:
                    child_indent = container_indent + 2
                    if pos < len(lines) and lines[pos][0] >= child_indent:
                        kind = _peek_kind(lines, pos, child_indent)
                        sub = [] if kind == "list" else {}
                        container[key] = sub
                        parse_block(sub, child_indent)
                    else:
                        container[key] = None
                continue
            # Dict value starting with a list item? unusual.
            list_match = _LIST_ITEM_RE.match(content)
            if list_match and content.lstrip().startswith("- "):
                # A bare list at dict-level. Convert this dict value into
                # a list at the appropriate key — but we don't know the
                # key here, so this branch is best-effort and only used
                # for the root level.
                pass
            pos += 1

    parse_block(root, -1)
    return root


def _peek_kind(lines: list[tuple[int, str]], pos: int, indent: int) -> str:
    """Look ahead from pos to see whether the block at `indent` is a list or mapping."""
    for i in range(pos, len(lines)):
        nxt_indent, nxt_content = lines[i]
        if nxt_indent < indent:
            return "mapping"
        if nxt_indent == indent:
            if nxt_content.startswith("- "):
                return "list"
            return "mapping"
        # deeper indent: keep scanning
    return "mapping"


def _parse_scalar(value: str) -> Any:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value in ("true", "True", "yes"):
        return True
    if value in ("false", "False", "no"):
        return False
    if value in ("null", "None", "~"):
        return None
    # Number?
    try:
        if "." in value or "e" in value or "E" in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


__all__ = [
    "PricingEntry",
    "PricingTable",
    "Profile",
    "load_pricing_profile",
    "compute_cost",
]