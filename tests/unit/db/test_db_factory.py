"""tests/unit/test_db_factory.py"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inhere_box.config import load_config
from inhere_box.infra.db.factory import open_database, should_defer_control_plane_db
from inhere_box.infra.utils.paths import PathLayout


def test_legacy_config_without_database_uses_paths_db(tmp_path: Path):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 9000}))
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert cfg.database_in_file is False
    pool = open_database(cfg, paths)
    assert pool.path == paths.db


def test_should_defer_when_sqlite_file_missing(tmp_path: Path):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 8088}))
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert should_defer_control_plane_db(cfg, paths) is True


def test_should_not_defer_when_sqlite_exists(tmp_path: Path):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 8088}))
    (root / "inhere-box.db").write_bytes(b"")
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert should_defer_control_plane_db(cfg, paths) is False


def test_should_not_defer_when_database_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 8088}))
    monkeypatch.setenv("INHERE_BOX_DATABASE_SQLITE_PATH", str(root / "env.db"))
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert should_defer_control_plane_db(cfg, paths) is False


def test_should_not_defer_postgresql(tmp_path: Path):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "database": {
                    "driver": "postgresql",
                    "host": "localhost",
                    "database": "octop",
                    "user": "octop",
                    "password": "x",
                }
            }
        )
    )
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert should_defer_control_plane_db(cfg, paths) is False


def test_sqlite_probe_does_not_create_missing_file(tmp_path: Path):
    from inhere_box.config import DatabaseConfig
    from inhere_box.infra.db.probe import probe_database

    root = tmp_path / ".octop"
    root.mkdir()
    paths = PathLayout(root)
    target = root / "data" / "fresh.db"
    assert not target.exists()
    probe_database(
        DatabaseConfig(driver="sqlite", sqlite_path="data/fresh.db"),
        paths,
    )
    assert not target.exists()
    assert target.parent.is_dir()


def test_config_with_database_section_uses_sqlite_path(tmp_path: Path):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps({"database": {"driver": "sqlite", "sqlite_path": "data/app.db"}})
    )
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert cfg.database_in_file is True
    pool = open_database(cfg, paths)
    assert pool.path == root / "data" / "app.db"


def test_env_sqlite_path_without_database_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 8088}))
    monkeypatch.setenv("INHERE_BOX_DATABASE_SQLITE_PATH", "/tmp/custom-inhere-box.db")
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    assert cfg.database_in_file is False
    pool = open_database(cfg, paths)
    assert pool.path == cfg.database.resolve_sqlite_path(paths.root)


def test_postgresql_returns_postgres_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Unit test without live server: monkeypatch PostgresPool.__init__ to no-op pool.
    from inhere_box.infra.db import factory as factory_mod
    from inhere_box.infra.db.pool import PostgresPool

    created: dict[str, str] = {}

    class FakePool(PostgresPool):
        def __init__(self, conninfo: str, **kwargs: object) -> None:
            created["conninfo"] = conninfo
            self.dialect = "postgresql"

        def close(self) -> None:
            return None

    monkeypatch.setattr(factory_mod, "PostgresPool", FakePool)

    root = tmp_path / ".octop"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "database": {
                    "driver": "postgresql",
                    "host": "localhost",
                    "database": "octop",
                    "user": "octop",
                    "password": "x",
                }
            }
        )
    )
    paths = PathLayout(root)
    cfg = load_config(paths.config)
    pool = open_database(cfg, paths)
    assert pool.dialect == "postgresql"
    assert "octop" in created["conninfo"]
