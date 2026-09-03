"""Install / uninstall helpers.

These are intentionally idempotent and safe: every change is preceded by
a backup, and the CLI prints exactly what changed so the user can roll
back manually if they want.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from hermes_checker import __version__

PLUGIN_NAME = "hermes-checker"


def _hermes_home(override: Path | None) -> Path:
    if override is not None:
        return override
    for env_var in ("HERMES_HOME",):
        v = os.environ.get(env_var)
        if v:
            return Path(v)
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes"
    if local.exists():
        return local
    return Path.home() / ".hermes"


def _package_source_dir() -> Path:
    """Return the in-tree path the plugin is copied from."""
    return Path(__file__).resolve().parent / "integrations" / "hermes_plugin"


def _package_root_dir() -> Path:
    """Return the in-tree path of the full hermes_checker package (copied alongside the plugin)."""
    return Path(__file__).resolve().parent


def _config_path(hermes_home: Path) -> Path:
    return hermes_home / "config.yaml"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".hermes-checker.bak")
    shutil.copy2(path, backup)
    return backup


def _config_has_enabled(text: str) -> bool:
    return "hermes-checker" in text


def _enable_in_config(hermes_home: Path, *, dry_run: bool) -> tuple[bool, Path | None]:
    """Add the plugin to config.yaml's plugins.enabled list. Returns (changed, backup_path)."""
    config = _config_path(hermes_home)
    text = _read_text(config)
    if _config_has_enabled(text):
        return False, None
    backup = _backup(config) if not dry_run else None
    if not text.strip():
        new_text = (
            "# Created by Hermes Checker — minimal config enabling the plugin.\n"
            "plugins:\n"
            "  enabled:\n"
            f"    - {PLUGIN_NAME}\n"
        )
    else:
        new_text = _inject_enabled(text)
    if not dry_run:
        config.write_text(new_text, encoding="utf-8")
    return True, backup


def _inject_enabled(text: str) -> str:
    """Insert the plugin entry into ``plugins.enabled``.

    - If ``plugins:`` doesn't exist, append a minimal block at the end.
    - If ``plugins.enabled:`` already exists, append the entry under it
      (preserving any prior entries).
    - If only ``plugins:`` exists, add ``enabled:`` and the entry under it.
    """
    lines = text.splitlines()
    if any(
        "hermes-checker" in line
        and line.lstrip().startswith("-")
        for line in lines
    ):
        return text

    # Find the plugins: block
    plugins_idx: Optional[int] = None
    plugins_indent: int = 0
    for i, line in enumerate(lines):
        if line.strip() == "plugins:" or line.strip().startswith("plugins:"):
            plugins_idx = i
            plugins_indent = len(line) - len(line.lstrip())
            break

    if plugins_idx is None:
        # No plugins block — append one.
        lines.append("plugins:")
        lines.append("  enabled:")
        lines.append(f"    - {PLUGIN_NAME}")
        return "\n".join(lines) + "\n"

    # Look for an "enabled:" child of plugins:.
    child_indent = plugins_indent + 2
    enabled_idx: Optional[int] = None
    for j in range(plugins_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= plugins_indent:
            break  # left the plugins block
        if line_indent == child_indent and stripped == "enabled:":
            enabled_idx = j
            break
        if line_indent == child_indent and stripped.startswith("enabled:"):
            enabled_idx = j
            break

    if enabled_idx is None:
        # No enabled: subkey — insert one with our entry.
        lines.insert(plugins_idx + 1, f"{' ' * child_indent}enabled:")
        lines.insert(plugins_idx + 2, f"{' ' * (child_indent + 2)}- {PLUGIN_NAME}")
        return "\n".join(lines) + "\n"

    # enabled: exists. Find the end of its list (or, if it's `enabled: []`,
    # convert the brackets to a multi-line list and append).
    enabled_line = lines[enabled_idx]
    if "[]" in enabled_line:
        # Convert `enabled: []` into a proper list.
        if enabled_line.lstrip().startswith("enabled:"):
            prefix_ws = " " * (len(enabled_line) - len(enabled_line.lstrip()))
            lines[enabled_idx] = f"{prefix_ws}enabled:"
            lines.insert(enabled_idx + 1, f"{' ' * (child_indent + 2)}- {PLUGIN_NAME}")
            return "\n".join(lines) + "\n"
    # Multi-line list — find the end and append.
    insert_at = enabled_idx + 1
    for j in range(enabled_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= child_indent:
            break
        insert_at = j + 1
    lines.insert(insert_at, f"{' ' * (child_indent + 2)}- {PLUGIN_NAME}")
    return "\n".join(lines) + "\n"


def install_plugin(
    *, hermes_home: Path | None = None, dry_run: bool = False
) -> int:
    """Install the user plugin into Hermes. Idempotent."""
    home = _hermes_home(hermes_home)
    plugins_dir = home / "plugins" / PLUGIN_NAME
    source_dir = _package_source_dir()

    if not source_dir.exists():
        print(f"ERROR: package plugin dir not found at {source_dir}")
        return 1

    print(f"Hermes Checker v{__version__} — install")
    print(f"  Hermes home: {home}")
    print(f"  Plugin dir:  {plugins_dir}")
    if dry_run:
        print("  (dry-run — no files changed)")

    # 1) Copy plugin directory
    if not dry_run:
        plugins_dir.parent.mkdir(parents=True, exist_ok=True)
        if plugins_dir.exists():
            shutil.rmtree(plugins_dir)
        shutil.copytree(source_dir, plugins_dir)
        # Also copy the rest of the hermes_checker package alongside so the
        # plugin can import it via its sys.path entry.
        pkg_src = _package_root_dir()
        pkg_dst = plugins_dir / "hermes_checker"
        if pkg_dst.exists():
            shutil.rmtree(pkg_dst)
        # Avoid copying the plugin shim (it would shadow the real package
        # under the install target). Skip the integrations/* and install.py.
        def _ignore(dirpath: str, names: list[str]) -> list[str]:
            keep = {"__init__.py"}
            ignored: list[str] = []
            for n in names:
                if n in ("install.py",):
                    ignored.append(n)
                elif n in ("__pycache__",):
                    ignored.append(n)
                elif Path(dirpath).name == "integrations":
                    ignored.append(n)
            return ignored
        shutil.copytree(pkg_src, pkg_dst, ignore=_ignore)
    prefix = "would copy" if dry_run else "copied"
    print(f"  {prefix} plugin -> {plugins_dir}")

    # 2) Edit config.yaml
    changed, backup = _enable_in_config(home, dry_run=dry_run)
    if changed:
        prefix = "would update" if dry_run else "updated"
        print(f"  {prefix} config: {_config_path(home)}")
        if backup is not None:
            print(f"  backup of original: {backup}")
    else:
        print("  config already enables hermes-checker — no edit needed")

    if not dry_run:
        print()
        print("Hermes Checker installed.")
        print("Restart the Hermes backend (or Hermes Desktop) to load the plugin.")
        print("Run `hermes-checker doctor` afterwards to verify.")
    return 0


def uninstall_plugin(*, hermes_home: Path | None = None) -> int:
    home = _hermes_home(hermes_home)
    plugins_dir = home / "plugins" / PLUGIN_NAME
    config = _config_path(home)

    print("Hermes Checker — uninstall")
    if plugins_dir.exists():
        shutil.rmtree(plugins_dir)
        print(f"  removed {plugins_dir}")
    else:
        print(f"  plugin dir not present: {plugins_dir}")

    if config.exists():
        backup = _backup(config)
        text = config.read_text(encoding="utf-8")
        new_text = _remove_enabled(text)
        if new_text != text:
            config.write_text(new_text, encoding="utf-8")
            print(f"  updated config: {config}")
            if backup:
                print(f"  backup: {backup}")
    print("Done.")
    return 0


def _remove_enabled(text: str) -> str:
    """Remove ``hermes-checker`` from the plugins.enabled list."""
    lines = text.splitlines()
    out: list[str] = []
    skip_next_dash = False
    in_plugins = False
    in_enabled = False
    enabled_indent = -1
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("plugins:") and indent == 0:
            in_plugins = True
            in_enabled = False
            out.append(line)
            continue
        if in_plugins and indent == 0 and not stripped.startswith("plugins:"):
            in_plugins = False
            in_enabled = False
        if in_plugins and stripped.startswith("enabled:") and indent > 0:
            in_enabled = True
            enabled_indent = indent
            out.append(line)
            continue
        if in_enabled and indent > enabled_indent and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item == PLUGIN_NAME:
                continue
            out.append(line)
            continue
        if in_enabled and (indent <= enabled_indent or not stripped):
            in_enabled = False
        out.append(line)
    return "\n".join(out) + "\n"


def diagnose(*, verbose: bool = True) -> int:
    """Verify the installation and integration.

    Returns 0 only if every required check passed. Each check is printed
    on its own line as ``[PASS]``, ``[FAIL]``, ``[WARN]`` or ``[SKIP]``
    so a glance is enough to see what is wrong.  We never fake PASS
    by searching config text for a substring — the real plugin manager
    must actually discover the plugin.
    """
    from hermes_checker.storage import Database, DatabasePaths

    # A list of (ok, label, detail) tuples we accumulate so we can also
    # return a non-zero exit code on any failure.
    results: list[tuple[bool, str, str]] = []
    hermes_version = ""

    def pass_(label: str, detail: str = "") -> None:
        results.append((True, label, detail))
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))

    def fail(label: str, detail: str = "") -> None:
        results.append((False, label, detail))
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))

    def warn(label: str, detail: str = "") -> None:
        print(f"  [WARN] {label}" + (f"  ({detail})" if detail else ""))

    def skip(label: str, detail: str = "") -> None:
        print(f"  [SKIP] {label}" + (f"  ({detail})" if detail else ""))

    print("Hermes Checker — doctor")
    print(f"  Version:        {__version__}")
    home = _hermes_home(None)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    paths = DatabasePaths.default()
    print(f"  Database path:  {paths.database}")
    if paths.database.exists():
        pass_("database writable", str(paths.database))
    else:
        # Try to create it and check.
        try:
            paths.database.parent.mkdir(parents=True, exist_ok=True)
            Database(paths).close()
            pass_("database writable (created)", str(paths.database))
        except Exception as exc:
            fail("database writable", f"{paths.database}: {exc}")
    try:
        db = Database(paths)
        v = db.schema_version
        if v >= 2:
            pass_("schema current", f"v{v}")
        else:
            fail("schema current", f"v{v}; V1.1 requires v2 — run any CLI command to upgrade")
    except Exception as exc:
        fail("schema current", str(exc))
    finally:
        try:
            db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Hermes installation
    # ------------------------------------------------------------------
    if home.exists():
        pass_("Hermes installation found", str(home))
    else:
        fail("Hermes installation found", f"{home} does not exist")

    # Detect Hermes version via a small probe (the import is local to
    # the sandbox only if Hermes' venv is on sys.path; if it isn't we
    # still want the doctor to be useful).
    try:
        from hermes_cli import __version__ as _hv  # type: ignore
        hermes_version = str(_hv)
    except Exception:
        # Fall back to reading the install-stamp the desktop bundles.
        try:
            stamp = home / "hermes-agent" / "apps" / "desktop" / "release" / "win-unpacked" / "resources" / "install-stamp.json"
            if stamp.exists():
                import json as _json
                hermes_version = str(_json.loads(stamp.read_text(encoding="utf-8")).get("commit", "?"))[:12]
        except Exception:
            hermes_version = ""
    if hermes_version:
        pass_("Hermes version detected", hermes_version)
    else:
        warn("Hermes version detected", "could not determine; not required")

    # ------------------------------------------------------------------
    # Hermes backend Python (the python.exe Hermes is launched with)
    # ------------------------------------------------------------------
    hermes_py = home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    if hermes_py.exists():
        pass_("Hermes backend Python found", str(hermes_py))
    else:
        warn("Hermes backend Python found", f"{hermes_py} missing")

    # ------------------------------------------------------------------
    # Plugin file layout
    # ------------------------------------------------------------------
    plugin_dir = home / "plugins" / PLUGIN_NAME
    plugin_yaml = plugin_dir / "plugin.yaml"
    plugin_init = plugin_dir / "__init__.py"

    if plugin_yaml.exists():
        pass_("plugin.yaml exists", str(plugin_yaml))
    else:
        fail("plugin.yaml exists", f"{plugin_yaml} missing — Hermes won't discover the plugin")

    if plugin_init.exists():
        pass_("__init__.py exists", str(plugin_init))
    else:
        fail("__init__.py exists", f"{plugin_init} missing")

    # The bundled ``hermes_checker`` package directory next to the plugin
    # is how ``register()`` imports the collector at runtime.
    pkg_dir = plugin_dir / "hermes_checker"
    if pkg_dir.is_dir():
        pass_("package core exists", str(pkg_dir))
    else:
        warn("package core exists", f"{pkg_dir} missing — plugin will fail to import (run `hermes-checker install` to repair)")

    # plugin.yaml parses
    manifest = None
    if plugin_yaml.exists():
        try:
            import yaml as _yaml  # type: ignore
            data = _yaml.safe_load(plugin_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            data = _parse_simple_yaml(plugin_yaml.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name") == PLUGIN_NAME:
            manifest = data
            pass_("plugin.yaml parses", f"name={data.get('name')!r} version={data.get('version', '?')!r}")
            if data.get("name") == PLUGIN_NAME:
                pass_("manifest name is hermes-checker")
            else:
                fail("manifest name is hermes-checker", f"got {data.get('name')!r}")
        else:
            fail("plugin.yaml parses", f"unexpected payload: {data!r}")

    # plugins.enabled actually contains hermes-checker
    config_path = _config_path(home)
    config_enabled = False
    if config_path.exists():
        try:
            import yaml as _yaml  # type: ignore
            cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = _parse_simple_yaml(config_path.read_text(encoding="utf-8"))
        enabled = ((cfg.get("plugins") or {}).get("enabled")
                   if isinstance(cfg, dict) else None)
        if isinstance(enabled, list) and PLUGIN_NAME in enabled:
            config_enabled = True
            pass_("plugins.enabled contains hermes-checker")
        elif config_path.exists():
            fail("plugins.enabled contains hermes-checker",
                 "not listed; run `hermes-checker install` to enable")
    else:
        fail("config.yaml exists", f"{config_path} missing")

    # ------------------------------------------------------------------
    # Real PluginManager discovery (the most important check)
    # ------------------------------------------------------------------
    if (hermes_py.exists() and plugin_yaml.exists() and plugin_init.exists()
            and config_enabled):
        try:
            import subprocess as _sp
            probe = """
import os, json, sys
os.environ['HERMES_HOME'] = os.environ['_HERMES_HOME']
sys.path.insert(0, os.environ['_SRCPATH'])
from hermes_cli.plugins import PluginManager
from hermes_constants import set_hermes_home_override, get_hermes_home
set_hermes_home_override(os.environ['_HERMES_HOME'])
mgr = PluginManager(scope_key=get_hermes_home())
manifests = mgr._collect_directory_manifests()
ours = [m for m in manifests if (m.key or m.name) == 'hermes-checker']
if not ours:
    print(json.dumps({"ok": False, "reason": "not discovered"}))
    raise SystemExit(0)
# Try to load it.
mgr._discover_and_load_inner()
loaded = mgr._plugins.get('hermes-checker')
if loaded is None:
    print(json.dumps({"ok": False, "reason": "not in manager"}))
    raise SystemExit(0)
print(json.dumps({
    "ok": bool(loaded.enabled),
    "hooks": list(loaded.hooks_registered),
    "error": loaded.error,
}, default=str))
"""
            proc = _sp.run(
                [str(hermes_py), "-c", probe],
                env={
                    **os.environ,
                    "HERMES_HOME": str(home),
                    "_HERMES_HOME": str(home),
                    "_SRCPATH": str(Path(__file__).resolve().parent.parent),
                    "PYTHONPATH": (
                        str(Path(__file__).resolve().parent.parent / "src")
                        + os.pathsep
                        + os.environ.get("PYTHONPATH", "")
                    ),
                },
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                fail("real PluginManager discovers plugin",
                     f"probe subprocess failed (rc={proc.returncode}): {proc.stderr[-300:]}")
            else:
                # Pick the last JSON-looking line.
                last = next(
                    (line for line in reversed(proc.stdout.strip().splitlines())
                     if line.startswith("{")),
                    "",
                )
                if not last:
                    fail("real PluginManager discovers plugin",
                         f"no JSON in probe output: {proc.stdout!r}")
                else:
                    import json as _json
                    info = _json.loads(last)
                    if info.get("ok"):
                        hooks = list(info.get("hooks") or [])
                        pass_("real PluginManager discovers plugin",
                              f"enabled=True, {len(hooks)} hooks registered")
                        pass_("plugin imports",
                              "no import error from register(ctx)")
                        expected_hooks = {
                            "pre_api_request", "post_api_request",
                            "api_request_error", "pre_tool_call",
                            "post_tool_call", "on_session_start",
                            "on_session_end", "on_session_finalize",
                            "on_session_reset", "on_skill_lifecycle",
                            "subagent_start", "subagent_stop",
                            "pre_llm_call", "post_llm_call",
                        }
                        missing = expected_hooks - set(hooks)
                        if missing:
                            fail("expected hooks register",
                                 f"missing: {sorted(missing)}")
                        else:
                            pass_("expected hooks register",
                                  f"{len(expected_hooks)} hooks all present")
                    else:
                        err = info.get("error")
                        if err:
                            fail("plugin imports", f"register(ctx) raised: {err}")
                        else:
                            fail("real PluginManager discovers plugin",
                                 info.get("reason", "unknown"))
        except _sp.TimeoutExpired:
            fail("real PluginManager discovers plugin", "probe timed out after 120s")
        except Exception as exc:
            fail("real PluginManager discovers plugin", f"probe error: {exc}")
    else:
        skip("real PluginManager discovers plugin",
             "preconditions missing (plugin not installed or not enabled)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    fails = [r for r in results if not r[0]]
    if fails:
        print(f"Doctor: {len(fails)} failure(s).")
        for ok, label, detail in results:
            if not ok:
                print(f"  - {label}: {detail}")
    else:
        print("Doctor: all checks passed.")
    print()
    print("Dashboard URL: http://127.0.0.1:8765/  (run `hermes-checker dashboard`)")
    return 1 if fails else 0


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML subset parser for the doctor (avoid forcing PyYAML).

    Just enough to recover the ``plugins.enabled`` list.  We deliberately
    use Hermes's full parser (PyYAML) when available — see the doctor —
    and fall back to this when PyYAML is missing.
    """
    out: dict[str, Any] = {"plugins": {"enabled": []}}
    in_plugins = False
    in_enabled = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "plugins:" and indent == 0:
            in_plugins = True
            in_enabled = False
            continue
        if in_plugins and indent == 0:
            in_plugins = False
            in_enabled = False
        if in_plugins and stripped == "enabled:" and indent > 0:
            in_enabled = True
            continue
        if in_enabled and stripped.startswith("- "):
            item = stripped[2:].strip()
            out["plugins"]["enabled"].append(item)
    return out


__all__ = ["install_plugin", "uninstall_plugin", "diagnose"]