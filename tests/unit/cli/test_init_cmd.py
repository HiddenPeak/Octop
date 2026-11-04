"""Unit tests for `inhere-box init`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from inhere_box.cli.main import cli


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    inhere_box_home = tmp_path / ".inhere-box"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("INHERE_BOX_HOME", str(inhere_box_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for key in list(os.environ):
        if key.startswith("INHERE_BOX_DATABASE_"):
            monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_init_non_interactive_creates_admin(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "init",
            "--admin-username",
            "alice",
            "--admin-password",
            "Wonderland1",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (fake_home / ".inhere-box").is_dir()
    assert (fake_home / ".inhere-box" / "inhere-box.db").is_file()
    # New catalog plugins are installed via the marketplace, not auto-seeded.
    weather = fake_home / ".inhere-box" / "plugins" / "weather" / "plugin.yaml"
    assert not weather.is_file()
    cfg = json.loads((fake_home / ".inhere-box" / "config.json").read_text(encoding="utf-8"))
    assert "weather" not in (cfg.get("plugins") or {})

    from inhere_box.infra.db.pool import SqlitePool
    from inhere_box.infra.db.repos.users import UserRepo
    from inhere_box.infra.utils.paths import PathLayout

    paths = PathLayout(fake_home / ".inhere-box")
    db = SqlitePool(paths.db)
    row = UserRepo(db).get_by_username("alice")
    assert row is not None
    assert row.role == "admin"


def test_init_allows_sidecar_files_without_database(fake_home: Path) -> None:
    """NAS / Docker shares often have login.txt before the first inhere-box.db exists."""
    home = fake_home / ".inhere-box"
    home.mkdir()
    (home / "inhere-box-login.txt").write_text("placeholder\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "init",
            "--admin-username",
            "alice",
            "--admin-password",
            "Wonderland1",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (home / "inhere-box.db").is_file()
    assert (home / "inhere-box-login.txt").read_text(encoding="utf-8") == "placeholder\n"


def test_init_refuses_to_overwrite_without_force(fake_home: Path) -> None:
    runner = CliRunner()
    args = [
        "init",
        "--admin-username",
        "alice",
        "--admin-password",
        "TestPass12",
        "--yes",
    ]
    r1 = runner.invoke(cli, args)
    assert r1.exit_code == 0
    r2 = runner.invoke(cli, args)
    assert r2.exit_code != 0
    out = (r2.output + (r2.stderr if hasattr(r2, "stderr") else "")).lower()
    assert "already" in out or "exists" in out


def test_init_force_resets(fake_home: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows may lock SQLite during init force reset")
    runner = CliRunner()
    args_a = [
        "init",
        "--admin-username",
        "alice",
        "--admin-password",
        "TestPass12",
        "--yes",
    ]
    args_b = [
        "init",
        "--force",
        "--admin-username",
        "bob",
        "--admin-password",
        "TestPass34",
        "--yes",
    ]
    r1 = runner.invoke(cli, args_a)
    assert r1.exit_code == 0, r1.output or str(r1.exception)
    r2 = runner.invoke(cli, args_b)
    assert r2.exit_code == 0, r2.output or str(r2.exception)

    from inhere_box.infra.db.pool import SqlitePool
    from inhere_box.infra.db.repos.users import UserRepo
    from inhere_box.infra.utils.paths import PathLayout

    paths = PathLayout(fake_home / ".inhere-box")
    repo = UserRepo(SqlitePool(paths.db))
    assert repo.get_by_username("bob") is not None


def test_init_rerun_skips_existing_admin_without_local_db(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL-style layout (no ``~/.inhere-box/inhere-box.db``): a rerun must not hit UNIQUE(username).

    Issue #1866: the Docker entrypoint re-invoked ``inhere-box init`` on every restart for
    external-DB backends, and the unconditional admin INSERT crashed with
    ``UniqueViolation`` / ``UNIQUE constraint failed``.
    """
    external = fake_home / "external"
    external.mkdir()
    monkeypatch.setenv("INHERE_BOX_DATABASE_DRIVER", "sqlite")
    monkeypatch.setenv("INHERE_BOX_DATABASE_SQLITE_PATH", str(external / "control.db"))
    runner = CliRunner()
    args = [
        "init",
        "--admin-username",
        "inhere-box",
        "--admin-password",
        "TestPass12",
        "--yes",
    ]
    r1 = runner.invoke(cli, args)
    assert r1.exit_code == 0, r1.output
    assert not (fake_home / ".inhere-box" / "inhere-box.db").exists()

    r2 = runner.invoke(cli, args)
    assert r2.exit_code == 0, r2.output or str(r2.exception)
    assert "already exists, skipping" in r2.output

    from inhere_box.infra.db.pool import SqlitePool
    from inhere_box.infra.db.repos.users import UserRepo

    assert UserRepo(SqlitePool(external / "control.db")).count() == 1


def test_init_password_too_short_rejects(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", "--admin-username", "a", "--admin-password", "abc", "--yes"],
    )
    assert result.exit_code != 0
    assert "password" in result.output.lower()


def test_init_env_vars_supply_credentials(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INHERE_BOX_ADMIN_USERNAME", "fromenv")
    monkeypatch.setenv("INHERE_BOX_ADMIN_PASSWORD", "EnvPass12")
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output

    from inhere_box.infra.db.pool import SqlitePool
    from inhere_box.infra.db.repos.users import UserRepo
    from inhere_box.infra.utils.paths import PathLayout

    paths = PathLayout(fake_home / ".inhere-box")
    repo = UserRepo(SqlitePool(paths.db))
    assert repo.get_by_username("fromenv") is not None
