"""Hermes-native prompt / context breakdown.

When Hermes Agent is installed on this machine, this module imports its
``hermes_cli.prompt_size.compute_prompt_breakdown`` and uses it offline
to attribute the FIXED prompt overhead that ships with every LLM call
(system identity, tools, skills, memory, user profile, MCP, subagent
definitions).

This is the preferred attribution path (Issue 5). The numbers are
``HERMES_NATIVE_ESTIMATE`` provenance — they are computed by the very
same code that builds Hermes's real prompts, so they accurately
attribute what every API call actually pays for.

When Hermes is not installed (or the import fails), we fall back to a
local estimator that tokenises whatever the collector happened to
cache in :class:`HookCollector`. The result is then labelled
``LOCALLY_ESTIMATED``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_checker import (
    PROVENANCE_HERMES_MEASURED,
    PROVENANCE_HERMES_NATIVE_ESTIMATE,
    PROVENANCE_LOCALLY_ESTIMATED,
    PROVENANCE_LOCALLY_CALCULATED,
    PROVENANCE_UNAVAILABLE,
)
from hermes_checker.accounting.tokenizer import Tokenizer, get_tokenizer

logger = logging.getLogger("hermes_checker.hermes_native")

# Honour HERMES_HOME so the wrapper can locate the right Hermes install.
_HERMES_NATIVE_CACHE: dict[str, "HermesNativeBridge"] = {}


@dataclass
class HermesNativeBridge:
    """Lazy wrapper around Hermes's offline prompt-size machinery.

    The first call to :meth:`compute` imports Hermes's
    ``hermes_cli.prompt_size`` module. If the import fails, every
    subsequent :meth:`compute` call returns ``hermes_native=False`` and
    ``reason`` explaining the failure; the caller falls back to local
    estimation.

    All results are offline — no provider, no LLM, no network.
    """

    hermes_home: Path
    _compute_fn: Optional[Any] = None
    _import_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._compute_fn is not None

    @property
    def import_error(self) -> Optional[str]:
        return self._import_error

    def _ensure_loaded(self) -> None:
        if self._compute_fn is not None or self._import_error is not None:
            return
        # Use Hermes's own hermes_home detection by setting HERMES_HOME
        # temporarily; the bridge NEVER mutates the user's actual config.
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        try:
            import importlib  # noqa: F401
            import sys
            # We need to import the Hermes-internal module without
            # inheriting any active process-level sys.path surprises. Use
            # a clean subprocess-style import via importlib by finding
            # the bundled Python.
            hermes_venv_python = self._hermes_python()
            hermes_root = self._hermes_root()
            if hermes_venv_python is None or hermes_root is None:
                raise RuntimeError(
                    f"Hermes not found at {self.hermes_home}; native "
                    f"breakdown disabled"
                )
            # We can call compute_prompt_breakdown via the hermes Python
            # interpreter. Spawning a subprocess on every snapshot is
            # wasteful, so we also offer an in-process mode for users
            # whose venv already has hermes-agent installed.
            if _is_in_hermes_venv(hermes_root):
                # We ARE in Hermes' own venv — just import.
                from hermes_cli.prompt_size import compute_prompt_breakdown
                self._compute_fn = compute_prompt_breakdown
            else:
                # Pre-build a small shim module that exposes the function
                # in a controlled way. We do this by importing the function
                # directly via importlib after prepending the Hermes
                # venv's site-packages to sys.path.
                site_dir = self._hermes_site_packages()
                if site_dir and str(site_dir) not in sys.path:
                    sys.path.insert(0, str(site_dir))
                from hermes_cli.prompt_size import compute_prompt_breakdown
                self._compute_fn = compute_prompt_breakdown
        except Exception as exc:
            self._import_error = f"{type(exc).__name__}: {exc}"
            logger.debug("hermes-native prompt_size import failed: %s", exc)

    def _hermes_python(self) -> Optional[Path]:
        """Absolute path to Hermes' venv Python, or ``None`` when absent."""
        candidates = [
            self.hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe",
            self.hermes_home / "hermes-agent" / "venv" / "bin" / "python",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    @property
    def hermes_python(self) -> Optional[Path]:
        return self._hermes_python()

    def _hermes_root(self) -> Optional[Path]:
        p = self.hermes_home / "hermes-agent"
        return p if p.exists() else None

    def _hermes_site_packages(self) -> Optional[Path]:
        if os.name == "nt":
            return self.hermes_home / "hermes-agent" / "venv" / "Lib" / "site-packages"
        sp = self.hermes_home / "hermes-agent" / "venv" / "lib"
        if not sp.exists():
            return None
        for child in sp.iterdir():
            if child.name.startswith("python") and (child / "site-packages").is_dir():
                return child / "site-packages"
        return None

    def compute(self, platform: str = "cli") -> "NativeBreakdown":
        """Run Hermes's offline prompt breakdown and translate its output."""
        self._ensure_loaded()
        if self._compute_fn is not None:
            try:
                data = self._invoke_compute(platform)
                return NativeBreakdown.from_hermes_payload(data)
            except Exception as exc:
                return NativeBreakdown(
                    hermes_native=False,
                    reason=f"in-process compute_prompt_breakdown raised: {exc}",
                    provenance=PROVENANCE_LOCALLY_ESTIMATED,
                )
        # Fall back to a Hermes-Venv subprocess so users who DON'T have
        # Hermes on PYTHONPATH can still get native numbers. The
        # subprocess is short-lived and offline — no provider, no LLM.
        if self.hermes_python is not None:
            return self._compute_via_subprocess(platform)
        return NativeBreakdown(
            hermes_native=False,
            reason=self._import_error or "hermes-cli prompt_size unavailable",
            provenance=PROVENANCE_LOCALLY_ESTIMATED,
        )

    def _invoke_compute(self, platform: str) -> Any:
        try:
            return self._compute_fn(platform=platform)  # type: ignore[call-arg]
        except TypeError:
            return self._compute_fn()  # type: ignore[call-arg]

    def _compute_via_subprocess(self, platform: str) -> "NativeBreakdown":
        """Invoke ``compute_prompt_breakdown`` in a Hermes-owned Python."""
        script = (
            "import os, sys, json\n"
            "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME', '')\n"
            "from hermes_cli.prompt_size import compute_prompt_breakdown\n"
            "data = compute_prompt_breakdown(os.environ.get('HERMES_PLATFORM', 'cli'))\n"
            "print(json.dumps(data, default=str))\n"
        )
        try:
            import subprocess
            proc = subprocess.run(
                [str(self.hermes_python), "-c", script],
                env={
                    **os.environ,
                    "HERMES_HOME": str(self.hermes_home),
                    "HERMES_PLATFORM": platform,
                },
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:
            return NativeBreakdown(
                hermes_native=False,
                reason=f"hermes subprocess wrapper failed: {exc}",
                provenance=PROVENANCE_LOCALLY_ESTIMATED,
            )
        if proc.returncode != 0:
            return NativeBreakdown(
                hermes_native=False,
                reason=f"hermes subprocess rc={proc.returncode}: "
                       f"{proc.stderr.strip()[-200:]}",
                provenance=PROVENANCE_LOCALLY_ESTIMATED,
            )
        try:
            import json
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return NativeBreakdown(
                hermes_native=False,
                reason=f"hermes subprocess output not JSON: {exc}",
                provenance=PROVENANCE_LOCALLY_ESTIMATED,
            )
        return NativeBreakdown.from_hermes_payload(data)


def _is_in_hermes_venv(hermes_root: Path) -> bool:
    try:
        import sys as _sys
        exe = Path(_sys.executable).resolve()
        venv_python = (hermes_root / "venv" / "Scripts" / "python.exe").resolve()
        return exe == venv_python
    except Exception:
        return False


def get_native_bridge(hermes_home: Optional[Path] = None) -> HermesNativeBridge:
    """Return a cached :class:`HermesNativeBridge` for the given home.

    If ``hermes_home`` is omitted, we try to discover the active Hermes
    home via Hermes's own ``hermes_constants.get_hermes_home``. The
    import is deferred and the bridge returns a "not available" instance
    if the import fails (we are running outside Hermes's venv).
    """
    if hermes_home is None:
        try:
            from hermes_constants import get_hermes_home  # type: ignore
            hermes_home = Path(get_hermes_home())
        except Exception:
            hermes_home = Path(os.environ.get("HERMES_HOME")
                              or (Path.home() / ".hermes"))
    key = str(hermes_home)
    bridge = _HERMES_NATIVE_CACHE.get(key)
    if bridge is None:
        bridge = HermesNativeBridge(hermes_home=hermes_home)
        _HERMES_NATIVE_CACHE[key] = bridge
    return bridge


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class NativeBreakdown:
    """Hermes-native prompt breakdown, ready to persist as a snapshot."""

    hermes_native: bool
    provenance: str
    reason: str = ""
    taken_at: float = field(default_factory=time.time)
    hermes_version: str = ""
    platform: str = ""
    model: str = ""
    base_url: str = ""
    tokenizer_method: str = "HEURISTIC"
    sections: list[dict[str, Any]] = field(default_factory=list)
    tiers: dict[str, dict[str, int]] = field(default_factory=dict)
    skills_index: dict[str, int] = field(default_factory=dict)
    memory: dict[str, int] = field(default_factory=dict)
    user_profile: dict[str, int] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    mcp_schemas: dict[str, int] = field(default_factory=dict)
    subagent_defs: dict[str, int] = field(default_factory=dict)
    other: dict[str, int] = field(default_factory=dict)
    skills_breakdown: list[dict[str, Any]] = field(default_factory=list)
    toolsets_breakdown: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_hermes_payload(cls, data: Mapping[str, Any]) -> "NativeBreakdown":
        """Translate Hermes's ``compute_prompt_breakdown`` output to our shape.

        Hermes returns::

            {
                "platform": "cli",
                "model": "...",
                "system_prompt": {"chars": ..., "bytes": ...},
                "skills_index": {"chars": ..., "bytes": ...},
                "memory":      {"chars": ..., "bytes": ...},
                "user_profile": {"chars": ..., "bytes": ...},
                "tools":       {"count": ..., "json_bytes": ...},
                "sections":    [["label", chars, bytes], ...],
                "skills_breakdown":    [{...}, ...],
                "toolsets_breakdown":  [{...}, ...],
            }

        Tier-by-label (stable/context/volatile) is recovered by walking
        the ``sections`` list and matching the well-known Hermes labels.
        """
        sections = [
            {"label": str(label), "chars": int(chars or 0), "bytes": int(bts or 0)}
            for label, chars, bts in data.get("sections", [])
        ]
        # Map tier names to the section dict whose label starts with the
        # tier. Hermes' labels are "stable (...)", "context (...)",
        # "volatile (...)" — match on the leading token.
        tier_names = {"stable", "context", "volatile"}
        tiers: dict[str, dict[str, int]] = {
            name: {"chars": 0, "bytes": 0, "tokens_est": 0}
            for name in tier_names
        }
        for section in sections:
            head = section["label"].split(" ", 1)[0].lower()
            if head in tier_names:
                tiers[head] = {
                    "chars": section["chars"],
                    "bytes": section["bytes"],
                    "tokens_est": 0,
                }

        def _num(d: Any, key: str, default: int = 0) -> int:
            if isinstance(d, Mapping):
                return int(d.get(key, default) or default)
            return default

        b = cls(
            hermes_native=True,
            provenance=PROVENANCE_HERMES_NATIVE_ESTIMATE,
            hermes_version="",
            platform=str(data.get("platform") or ""),
            model=str(data.get("model") or ""),
            base_url="",
            sections=sections,
            tiers=tiers,
            skills_index={
                "chars": _num(data.get("skills_index"), "chars"),
                "bytes": _num(data.get("skills_index"), "bytes"),
            },
            memory={
                "chars": _num(data.get("memory"), "chars"),
                "bytes": _num(data.get("memory"), "bytes"),
            },
            user_profile={
                "chars": _num(data.get("user_profile"), "chars"),
                "bytes": _num(data.get("user_profile"), "bytes"),
            },
            tools={
                "count": _num(data.get("tools"), "count"),
                "json_bytes": _num(data.get("tools"), "json_bytes"),
            },
            mcp_schemas={"chars": 0, "bytes": 0},
            subagent_defs={"chars": 0, "bytes": 0},
            other={"chars": 0, "bytes": 0},
            skills_breakdown=list(data.get("skills_breakdown", [])),
            toolsets_breakdown=list(data.get("toolsets_breakdown", [])),
            metadata={"source": "hermes_cli.prompt_size.compute_prompt_breakdown"},
        )
        b._hydrate_token_estimates()
        return b

    def _hydrate_token_estimates(self) -> None:
        """Apply our local tokenizer to every byte count Hermes returned.

        Hermes's own breakdown is character-precise but does not emit
        token counts (it would need a tokenizer that matches every
        provider). We do that here, using the same tokenizer Hermes
        Checker uses elsewhere.
        """
        tokenizer = get_tokenizer(self.model or None)
        self.tokenizer_method = tokenizer.count("hello").method

        def _est(d: dict[str, int], *, key: str = "chars") -> None:
            if not d or d.get("tokens_est", 0):
                return
            # Hermes returns both chars and bytes; prefer chars when
            # available (close to the user's reading), fall back to bytes.
            n = int(d.get(key, 0) or 0)
            if not n:
                n = int(d.get("bytes", 0) or 0)
            d["tokens_est"] = tokenizer.count("x" * n).tokens

        for k in list(self.tiers.keys()):
            _est(self.tiers[k])
        for k in ("skills_index", "memory", "user_profile", "mcp_schemas",
                  "subagent_defs", "other"):
            _est(getattr(self, k))

        # skills_breakdown uses bytes, not chars (Hermes reports the
        # compact size of the skill index line and the full SKILL.md
        # file in bytes).  We tokenise by building a string of that
        # many characters — the token count is approximate but stable.
        for s in self.skills_breakdown:
            for src_key, dst_key in (
                ("index_line_bytes", "index_line_tokens_est"),
                ("skill_md_bytes", "skill_md_tokens_est"),
            ):
                if s.get(dst_key):
                    continue
                n = int(s.get(src_key, 0) or 0)
                if n:
                    s[dst_key] = tokenizer.count("x" * n).tokens
        for t in self.toolsets_breakdown:
            if t.get("schema_tokens_est"):
                continue
            n = int(t.get("schema_bytes", 0) or 0) or int(
                t.get("json_bytes", 0) or 0
            ) or int(
                t.get("schema_chars", 0) or 0
            )
            if n:
                t["schema_tokens_est"] = tokenizer.count("x" * n).tokens

        if isinstance(self.tools, dict) and self.tools.get("json_bytes", 0):
            self.tools["json_tokens_est"] = tokenizer.count(
                "x" * int(self.tools["json_bytes"])
            ).tokens


# ---------------------------------------------------------------------------
# Standalone fallback when Hermes is not importable
# ---------------------------------------------------------------------------


def local_estimate_from_payload(
    messages: list[Mapping[str, Any]],
    *,
    model: Optional[str] = None,
) -> "NativeBreakdown":
    """Best-effort local breakdown when Hermes-native is unavailable.

    Tokenises the captured ``messages`` payload and emits a simple
    SYSTEM/USER/ASSISTANT/TOOL split. This is the LAST-RESORT path and
    the result is tagged ``LOCALLY_ESTIMATED``.
    """
    tokenizer = get_tokenizer(model)
    b = NativeBreakdown(
        hermes_native=False,
        provenance=PROVENANCE_LOCALLY_ESTIMATED,
        model=model or "",
        tokenizer_method=tokenizer.count("x").method,
        reason="Hermes-native breakdown unavailable; local fallback.",
    )
    accumulator: dict[str, dict[str, int]] = {}
    for m in messages:
        role = (m.get("role") or "OTHER").lower()
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        c = accumulator.setdefault(role, {"chars": 0, "bytes": 0, "tokens_est": 0})
        tc = tokenizer.count(text)
        c["chars"] += tc.text_chars
        c["bytes"] += tc.text_bytes
        c["tokens_est"] += tc.tokens
    b.metadata = {"local_fallback": True, "message_count": len(messages)}
    b.tiers = {
        "stable": {"chars": 0, "bytes": 0, "tokens_est": 0},
        "context": {"chars": 0, "bytes": 0, "tokens_est": 0},
        "volatile": accumulator.get("user", {"chars": 0, "bytes": 0, "tokens_est": 0}),
    }
    b.memory = accumulator.get("memory", {"chars": 0, "bytes": 0, "tokens_est": 0})
    b.user_profile = accumulator.get("user_profile", {"chars": 0, "bytes": 0, "tokens_est": 0})
    b.skills_index = accumulator.get("skills", {"chars": 0, "bytes": 0, "tokens_est": 0})
    b.other = accumulator.get("other", {"chars": 0, "bytes": 0, "tokens_est": 0})
    b.skills_breakdown = []
    b.toolsets_breakdown = []
    b.sections = [
        {"label": role, "chars": v["chars"], "bytes": v["bytes"]}
        for role, v in accumulator.items()
    ]
    return b


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            (p.get("text", "") if isinstance(p, Mapping) and p.get("type") == "text"
             else p if isinstance(p, str)
             else str(p))
            for p in content
        )
    if isinstance(content, Mapping):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


__all__ = [
    "HermesNativeBridge",
    "NativeBreakdown",
    "get_native_bridge",
    "local_estimate_from_payload",
]


# Resolve the provenance constants from the package root if not exported.
try:
    from hermes_checker import (  # type: ignore  # noqa: F401
        PROVENANCE_HERMES_MEASURED,
        PROVENANCE_HERMES_NATIVE_ESTIMATE,
        PROVENANCE_LOCALLY_ESTIMATED,
        PROVENANCE_LOCALLY_CALCULATED,
        PROVENANCE_UNAVAILABLE,
    )
except ImportError:  # pragma: no cover
    pass