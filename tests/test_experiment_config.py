"""Tests for the persistent experiment config (Issue 16)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_checker.storage import Database, DatabasePaths


def test_app_config_round_trip(tmp_path: Path) -> None:
    paths = DatabasePaths.from_path(tmp_path / "app.db")
    db = Database(paths)
    # Issue 16: experiment label is persisted in app_config and read back.
    db.set_app_config("experiment", "baseline-minimax-direct")
    assert db.get_app_config("experiment") == "baseline-minimax-direct"
    # The collector's HookCollector picks it up via config.experiment_label.
    from hermes_checker.collector.config import CollectorConfig
    cfg = CollectorConfig(
        database_path=str(paths.database),
        experiment_label=db.get_app_config("experiment"),
    )
    assert cfg.experiment_label == "baseline-minimax-direct"

    # Round-trip a second time: set / clear / set.
    db.set_app_config("experiment", "deepseek-openrouter")
    assert db.get_app_config("experiment") == "deepseek-openrouter"
    db.set_app_config("experiment", "")
    assert db.get_app_config("experiment") == ""
    db.set_app_config("experiment", "omniroute-rtk")
    assert db.get_app_config("experiment") == "omniroute-rtk"
