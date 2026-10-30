"""Deployment mode is explicit and a box never enters the old DB setup wizard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.config import load_config
from octop.infra.db.factory import should_defer_control_plane_db
from octop.infra.utils.paths import PathLayout


def test_box_mode_loads_from_file_and_prepares_its_database(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"account_mode": "box_local"}), encoding="utf-8")
    config = load_config(config_path)
    assert config.account_mode == "box_local"
    assert not should_defer_control_plane_db(config, PathLayout(tmp_path))


def test_account_mode_default_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    assert load_config(config_path).account_mode == "server"
    monkeypatch.setenv("OCTOP_ACCOUNT_MODE", "box_local")
    assert load_config(config_path).account_mode == "box_local"
    monkeypatch.setenv("OCTOP_ACCOUNT_MODE", "unknown")
    with pytest.raises(ValueError, match="account_mode"):
        load_config(config_path)


@pytest.mark.parametrize("value", [None, "local", 1, True, ["box_local"]])
def test_invalid_account_mode_fails_instead_of_falling_back(tmp_path: Path, value: object) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"account_mode": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="account_mode"):
        load_config(config_path)
