"""Real Hermes integration tests.

These tests exercise the ACTUAL Hermes Python interpreter and the
ACTUAL Hermes source tree — not a mock. They are skipped automatically
on machines that do not have Hermes installed.

The first test confirms the live PluginManager discovers the
in-tree hermes-checker plugin and that ``register(ctx)`` runs. The
second test confirms Hermes's ``compute_prompt_breakdown`` produces a
parseable breakdown through our bridge.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERMES_PYTHON = Path(
    r"C:\Users\xampos\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
)
HERMES_ROOT = Path(r"C:\Users\xampos\AppData\Local\hermes\hermes-agent")
HERMES_HOME = Path(r"C:\Users\xampos\AppData\Local\hermes")


pytestmark = pytest.mark.skipif(
    not HERMES_PYTHON.exists() or not HERMES_ROOT.exists(),
    reason="Hermes Agent is not installed at the standard location on this machine",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _isolated_hermes_home(tmp: Path) -> Path:
    """Build a fresh isolated HERMES_HOME so the test cannot touch the real install."""
    # Use the real config.yaml from the live install — that exercises the
    # same config path the user does, but in a sandbox.
    sandbox = tmp / "sandbox_hermes"
    sandbox.mkdir(parents=True, exist_ok=True)
    if (HERMES_HOME / "config.yaml").exists():
        shutil.copy2(HERMES_HOME / "config.yaml", sandbox / "config.yaml")
    (sandbox / "plugins").mkdir(exist_ok=True)
    (sandbox / "skills").mkdir(exist_ok=True)
    return sandbox


def test_real_hermes_plugin_manager_discovers_hermes_checker(tmp_path: Path) -> None:
    """Run the actual Hermes PluginManager against an isolated HERMES_HOME.

    This test:
      1. Sets HERMES_HOME to an isolated sandbox.
      2. Installs the in-tree hermes-checker plugin there.
      3. Invokes ``hermes_cli.plugins.discover_plugins()`` and
         ``_load_plugin_scoped()`` on the discovered manifest.
      4. Asserts the plugin is enabled, registers all listed hooks, and
         runs ``register(ctx)`` without import errors.
    """
    sandbox = _isolated_hermes_home(tmp_path)
    plugin_dir = sandbox / "plugins" / "hermes-checker"

    # Copy the in-tree plugin tree (plugin.yaml + __init__.py) into the
    # sandbox.  We deliberately do NOT copy the full hermes_checker/
    # package alongside, because the test machine already has it on
    # sys.path (via the test process) and we want register() to find it.
    src_plugin = _project_root() / "src" / "hermes_checker" / "integrations" / "hermes_plugin"
    shutil.copytree(src_plugin, plugin_dir)

    # Add the project source to PYTHONPATH so register() can import
    # hermes_checker.collector (just like the install command does in
    # production).
    env = os.environ.copy()
    env["HERMES_HOME"] = str(sandbox)
    env["PYTHONPATH"] = (
        str(_project_root() / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )

    # Inject the plugin into the live opt-in allow-list so the manager
    # will load it.
    config = sandbox / "config.yaml"
    text = config.read_text(encoding="utf-8")
    if "hermes-checker" not in text:
        if "plugins:" in text:
            text = text.replace("plugins:\n  enabled: []",
                                "plugins:\n  enabled:\n    - hermes-checker")
        else:
            text = text.rstrip() + "\nplugins:\n  enabled:\n    - hermes-checker\n"
        config.write_text(text, encoding="utf-8")

    script = """
import os
import sys
import json

# Force the isolated sandbox home.
os.environ['HERMES_HOME'] = os.environ['_HERMES_HOME']
sys.path.insert(0, os.environ['_SRCPATH'])

from hermes_cli.plugins import PluginManager
from hermes_constants import set_hermes_home_override, get_hermes_home

set_hermes_home_override(os.environ['_HERMES_HOME'])

# Build a fresh manager bound to the sandbox home so cross-process cache
# doesn't bite.  Discover manifests directly without invoking the full
# load pipeline (so we can inspect them before register() runs).
mgr = PluginManager(scope_key=get_hermes_home())
manifests = mgr._collect_directory_manifests()
user_manifest = next(
    (m for m in manifests if (m.key or m.name) == "hermes-checker"),
    None,
)
if user_manifest is None:
    print(json.dumps({"ok": False, "reason": "not discovered",
                      "names": [(m.key or m.name) for m in manifests]}))
    sys.exit(0)

# Now run the real load pipeline. This will exec_module the plugin
# __init__.py, call register(ctx), and attribute tool/hook registrations.
mgr._discover_and_load_inner()
loaded = mgr._plugins.get(user_manifest.key or user_manifest.name)
if loaded is None:
    print(json.dumps({"ok": False, "reason": "not in manager"}))
    sys.exit(0)

print(json.dumps({
    "ok": bool(loaded.enabled),
    "enabled": loaded.enabled,
    "hooks": list(loaded.hooks_registered),
    "tools": list(loaded.tools_registered),
    "error": loaded.error,
}, default=str))
"""
    result = subprocess.run(
        [str(HERMES_PYTHON), "-c", script],
        env={
            **env,
            "_HERMES_HOME": str(sandbox),
            "_SRCPATH": str(_project_root() / "src"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Hermes subprocess failed (rc={result.returncode})\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    last_line = result.stdout.strip().splitlines()[-1]
    import json
    info = json.loads(last_line)
    assert info["ok"], f"Hermes did not enable hermes-checker: {info}"
    expected_hooks = {
        "pre_api_request", "post_api_request", "api_request_error",
        "pre_tool_call", "post_tool_call",
        "on_session_start", "on_session_end", "on_session_finalize",
        "on_session_reset", "on_skill_lifecycle",
        "subagent_start", "subagent_stop",
        "pre_llm_call", "post_llm_call",
    }
    missing = expected_hooks - set(info["hooks"])
    assert not missing, f"Hermes did not register hooks: {sorted(missing)}"
    assert info["error"] is None, f"register() raised: {info['error']}"


def test_real_hermes_compute_prompt_breakdown_through_bridge() -> None:
    """Run Hermes's offline breakdown through our bridge and check tokens.

    This proves our adapter translates Hermes's dict shape correctly
    AND that the local tokenizer pass yields plausible token counts.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(_project_root() / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    script = """
import json
import os
import sys
sys.path.insert(0, os.environ['_SRCPATH'])
from pathlib import Path
from hermes_checker.hermes_native import get_native_bridge

bridge = get_native_bridge(Path(os.environ['_HERMES_HOME']))
result = bridge.compute('cli')
print(json.dumps({
    "hermes_native": result.hermes_native,
    "reason": result.reason,
    "tiers": result.tiers,
    "tools": result.tools,
    "skills_index": result.skills_index,
    "memory": result.memory,
    "user_profile": result.user_profile,
    "provenance": result.provenance,
}))
"""
    result = subprocess.run(
        [str(HERMES_PYTHON), "-c", script],
        env={
            **env,
            "_HERMES_HOME": str(HERMES_HOME),
            "_SRCPATH": str(_project_root() / "src"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Hermes subprocess failed (rc={result.returncode})\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
    import json
    info = json.loads(result.stdout.strip().splitlines()[-1])
    # The real Hermes IS installed, so the bridge should succeed.
    assert info["hermes_native"] is True, f"bridge did not load: {info}"
    # Token estimates must be populated by our local tokenizer pass.
    assert info["tiers"]["stable"]["tokens_est"] > 0
    assert info["tools"]["json_tokens_est"] > 0
    assert info["skills_index"]["tokens_est"] > 0
    assert info["memory"]["tokens_est"] > 0
    assert info["user_profile"]["tokens_est"] > 0
    assert info["provenance"] == "HERMES_NATIVE_ESTIMATE"