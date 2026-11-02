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


def test_box_http_origins_requires_explicit_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    assert load_config(path).box_http_origins == []
    path.write_text(
        json.dumps({"box_http_origins": ["http://box.example:10188", "http://192.0.2.1:8080"]})
    )
    assert load_config(path).box_http_origins == [
        "http://box.example:10188",
        "http://192.0.2.1:8080",
    ]


@pytest.mark.parametrize(
    "value",
    [
        True,
        None,
        1,
        "https://box.test",
        "http://box.test/",
        "http://u:p@box.test",
        "http://box.test?secret=x",
        "http://box.test#fragment",
        "http://box.test:70000",
        "http://box.test:0",
        "http://[invalid",
        "http://box.test:bad",
        "http://",
        " http://box.test",
        "http://box.\ntest",
    ],
)
def test_invalid_box_http_origins_fails_closed(tmp_path: Path, value: object) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"box_http_origins": [value]}))
    with pytest.raises(ValueError, match="box_http_origins"):
        load_config(path)


@pytest.mark.parametrize("value", [None, True, "http://box.test", {}])
def test_box_http_origins_rejects_non_list(tmp_path: Path, value: object) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"box_http_origins": value}))
    with pytest.raises(ValueError, match="box_http_origins"):
        load_config(path)
