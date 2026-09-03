"""Tests for the Hermes plugin manifest + plugin install layout.

These tests guard against the V1 review finding that
``hermes_checker/integrations/hermes_plugin/`` shipped without
``plugin.yaml`` and was therefore NOT discoverable by Hermes's
``PluginManager``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# The installed plugin's __init__.py lives in this directory.
HERMES_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "src" / "hermes_checker" / "integrations" / "hermes_plugin"


def test_plugin_yaml_exists_in_source() -> None:
    """The source tree MUST include plugin.yaml so the wheel ships it."""
    assert (HERMES_PLUGIN_DIR / "plugin.yaml").exists(), (
        "Missing plugin.yaml — Hermes's PluginManager will not discover "
        "hermes-checker without it. See docs/HERMES_DISCOVERY.md."
    )


def test_plugin_yaml_has_required_fields() -> None:
    """Verify the manifest carries the fields Hermes parses."""
    text = (HERMES_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    # The manifest MUST have a name. The directory name is the registry key.
    assert re.search(r"^name:\s*\S", text, re.MULTILINE), "manifest missing 'name:'"
    # A version is recommended for `hermes plugins list` and install safety.
    assert re.search(r"^version:\s*\S", text, re.MULTILINE), "manifest missing 'version:'"
    # Hermes parses the 'hooks:' list to drive `hermes plugins show`. The
    # entries must be valid hook names.
    assert re.search(r"^hooks:\s*(?:#[^\n]*)?$", text, re.MULTILINE), (
        "manifest missing 'hooks:' list (even an empty list is fine, but "
        "every hermes-checker callback must be listed)"
    )
    # Plugins ship with kind=standalone by convention.
    assert re.search(r"^kind:\s*standalone\s*$", text, re.MULTILINE), (
        "manifest must declare kind: standalone (the default for observer "
        "plugins that only attach lifecycle hooks)"
    )


def test_plugin_init_importable() -> None:
    """The ``__init__.py`` next to plugin.yaml must define ``register``."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_hermes_plugin", HERMES_PLUGIN_DIR / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "register"), "plugin __init__.py must define register(ctx)"
    assert callable(module.register), "register must be callable"


def test_plugin_listed_hooks_cover_every_callback() -> None:
    """The YAML ``hooks:`` list MUST list every hook we register.

    Otherwise ``hermes plugins show hermes-checker`` will not show all
    callbacks and operators will miss the ones the plugin actually wires.
    """
    from importlib import util as importlib_util

    init_spec = importlib_util.spec_from_file_location(
        "_hermes_plugin_init", HERMES_PLUGIN_DIR / "__init__.py"
    )
    init_module = importlib_util.module_from_spec(init_spec)
    init_spec.loader.exec_module(init_module)

    # Find every ctx.register_hook("X", ...) call inside register()
    src = (HERMES_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'register_hook\(\s*["\']([a-z_]+)["\']\s*,', src))
    assert registered, "plugin registers no hooks"

    yaml_text = (HERMES_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    yaml_hooks = set(re.findall(r"^\s*-\s*([a-z_]+)\s*$", yaml_text, re.MULTILINE))
    # Every registered hook should be listed in the YAML.
    missing = registered - yaml_hooks
    assert not missing, (
        f"plugin.yaml must list every hook the plugin registers. "
        f"Missing: {sorted(missing)}"
    )


@pytest.mark.parametrize("path", [
    "src/hermes_checker/integrations/hermes_plugin/__init__.py",
    "src/hermes_checker/integrations/hermes_plugin/plugin.yaml",
])
def test_plugin_files_exist_in_repo(path: str) -> None:
    repo = Path(__file__).resolve().parent.parent
    assert (repo / path).exists(), f"missing required file: {path}"


def test_plugin_yaml_round_trips_via_pyyaml() -> None:
    """If PyYAML is present, the manifest must round-trip cleanly.

    Hermes itself uses ``yaml.safe_load`` to parse the manifest, so any
    YAML our plugin.yaml doesn't parse cleanly is a discovery failure.
    """
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((HERMES_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["name"] == "hermes-checker"
    assert isinstance(data.get("hooks"), list)
    assert "pre_api_request" in data["hooks"]
    assert "post_api_request" in data["hooks"]
