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
from typing import Optional

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
    """Verify the installation and integration."""
    from hermes_checker.storage import Database, DatabasePaths

    print("Hermes Checker — doctor")
    print(f"  Version:        {__version__}")
    paths = DatabasePaths.default()
    print(f"  Database path:  {paths.database}")
    print(f"  Database exists: {paths.database.exists()}")
    try:
        db = Database(paths)
        print(f"  Schema version: {db.schema_version}")
    except Exception as exc:
        print(f"  Database ERROR: {exc}")
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass

    home = _hermes_home(None)
    print(f"  Hermes home:    {home}  ({'exists' if home.exists() else 'missing'})")
    plugin_dir = home / "plugins" / PLUGIN_NAME
    print(f"  Plugin dir:     {plugin_dir}  ({'present' if plugin_dir.exists() else 'NOT INSTALLED'})")

    config = _config_path(home)
    if config.exists():
        text = config.read_text(encoding="utf-8")
        enabled = _config_has_enabled(text)
        print(f"  config.yaml:    {config}  (hermes-checker enabled: {enabled})")
    else:
        print(f"  config.yaml:    {config}  (missing)")

    # Sessions recorded
    db2 = Database(paths)
    sessions = db2.sessions(limit=1000)
    print(f"  Sessions recorded: {len(sessions)}")
    if sessions:
        s = sessions[0]
        print(f"  Latest session:    {s['session_id']}  (started {s['started_at']:.0f})")
        n_requests = len(db2.api_requests_for_session(s["session_id"]))
        n_tools = len(db2.tool_calls_for_session(s["session_id"]))
        print(f"    LLM requests:    {n_requests}")
        print(f"    Tool calls:      {n_tools}")
    db2.close()
    print()
    print("Dashboard URL: http://127.0.0.1:8765/  (run `hermes-checker dashboard`)")
    return 0


__all__ = ["install_plugin", "uninstall_plugin", "diagnose"]