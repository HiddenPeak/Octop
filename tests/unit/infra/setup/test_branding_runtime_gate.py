"""Runtime entrypoints must never create a fresh install beside unmigrated data."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from inhere_box.cli.main import cli
from inhere_box.config import DatabaseConfig, InhereBoxConfig
from inhere_box.infra.db import factory
from inhere_box.infra.setup.branding_migration import (
    BrandingMigrationError,
    require_directory_migrated,
    require_installation_migrated,
)
from inhere_box.infra.utils.paths import PathLayout, default_root


def _legacy(parent: Path) -> tuple[Path, Path]:
    source, target = parent / ".octop", parent / ".inhere-box"
    source.mkdir()
    (source / "octop.db").write_bytes(b"preserve existing installation")
    return source, target


def _journal(
    parent: Path, source: Path, target: Path, phase: str, *, database_present: bool = False
) -> Path:
    journal = parent / ".inhere-box-directory-migration.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "source": str(source),
                "destination": str(target),
                "staging": str(parent / ".inhere-box-staging-test"),
                "backup": str(parent / ".octop.before-inhere-box-test"),
                "token": "a" * 32,
                "phase": phase,
                "database_present": None if phase == "building" else database_present,
            }
        ),
        encoding="utf-8",
    )
    return journal


def test_default_root_never_selects_old_directory(tmp_path: Path) -> None:
    _, target = _legacy(tmp_path)
    assert default_root(tmp_path) == target
    assert not target.exists()


@pytest.mark.parametrize("selected", ["old", "new"])
def test_old_installation_requires_directory_migration_without_writes(
    tmp_path: Path, selected: str
) -> None:
    source, target = _legacy(tmp_path)
    root = source if selected == "old" else target
    with pytest.raises(BrandingMigrationError, match="--destination"):
        require_installation_migrated(root)
    assert (source / "octop.db").read_bytes() == b"preserve existing installation"
    assert list(source.iterdir()) == [source / "octop.db"]
    assert not target.exists()


@pytest.mark.parametrize("phase", ["building", "ready", "published"])
@pytest.mark.parametrize("exists", [False, True])
def test_incomplete_directory_migration_blocks_destination_even_after_source_archival(
    tmp_path: Path, phase: str, exists: bool
) -> None:
    source, target = tmp_path / ".octop", tmp_path / ".inhere-box"
    if exists:
        target.mkdir()
    journal = _journal(tmp_path, source, target, phase)
    before = journal.read_bytes()
    with pytest.raises(BrandingMigrationError, match="--destination"):
        require_directory_migrated(target)
    assert journal.read_bytes() == before
    assert target.exists() is exists


def test_completed_directory_migration_and_archived_source_do_not_block_runtime(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / ".octop", tmp_path / ".inhere-box"
    target.mkdir()
    (tmp_path / ".octop.before-inhere-box-test").mkdir()
    _journal(tmp_path, source, target, "complete")
    (target / ".inhere-box-directory-migration-owner").write_text("a" * 32, encoding="ascii")
    require_installation_migrated(target)


@pytest.mark.parametrize("missing", ["destination", "renamed", "owner", "owner-token", "database"])
def test_completed_receipt_never_authorizes_reinitializing_lost_migration_data(
    tmp_path: Path, missing: str
) -> None:
    source, target = tmp_path / ".octop", tmp_path / ".inhere-box"
    target.mkdir()
    owner = target / ".inhere-box-directory-migration-owner"
    owner.write_text("a" * 32, encoding="ascii")
    database = target / "inhere-box.db"
    database.write_bytes(b"existing migrated database")
    journal = _journal(tmp_path, source, target, "complete", database_present=True)
    if missing == "destination":
        owner.unlink()
        database.unlink()
        target.rmdir()
    elif missing == "renamed":
        target.rename(tmp_path / "displaced-migrated-installation")
    elif missing == "owner":
        owner.unlink()
    elif missing == "owner-token":
        owner.write_text("different owner", encoding="ascii")
    else:
        database.unlink()
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    config = InhereBoxConfig()
    for operation in (factory.open_database, factory.should_defer_control_plane_db):
        with pytest.raises(BrandingMigrationError, match="destination is missing or changed"):
            operation(config, PathLayout(target))
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    assert journal.is_file()
    assert target.exists() is (missing not in {"destination", "renamed"})


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
@pytest.mark.parametrize("linked", ["owner", "database", "destination"])
def test_completed_destination_requires_real_regular_files(tmp_path: Path, linked: str) -> None:
    source, target = tmp_path / ".octop", tmp_path / ".inhere-box"
    target.mkdir()
    owner = target / ".inhere-box-directory-migration-owner"
    owner.write_text("a" * 32, encoding="ascii")
    database = target / "inhere-box.db"
    database.write_bytes(b"migrated database")
    _journal(tmp_path, source, target, "complete", database_present=True)
    if linked == "destination":
        outside = tmp_path / "outside"
        target.rename(outside)
        target.symlink_to(outside, target_is_directory=True)
    else:
        path = owner if linked == "owner" else database
        outside = tmp_path / ("outside-" + linked)
        path.rename(outside)
        path.symlink_to(outside)
    with pytest.raises(BrandingMigrationError, match="destination is missing or changed"):
        require_installation_migrated(target)


def test_completed_destination_with_matching_owner_and_database_remains_usable(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / ".octop", tmp_path / ".inhere-box"
    target.mkdir()
    (target / ".inhere-box-directory-migration-owner").write_text("a" * 32, encoding="ascii")
    (target / "inhere-box.db").write_bytes(b"migrated database")
    _journal(tmp_path, source, target, "complete", database_present=True)
    require_installation_migrated(target)


def test_fresh_installation_without_directory_receipt_is_unchanged(tmp_path: Path) -> None:
    target = tmp_path / ".inhere-box"
    require_installation_migrated(target)
    assert not target.exists()


def test_unrelated_directory_journal_and_nested_sdk_directory_do_not_block_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".inhere-box"
    sdk = root / "agents" / "one" / ".octop"
    sdk.mkdir(parents=True)
    (sdk / "octop.db").write_bytes(b"SDK-owned data")
    _journal(tmp_path, tmp_path / "different-source", tmp_path / "different-target", "building")
    require_installation_migrated(root)
    assert (sdk / "octop.db").read_bytes() == b"SDK-owned data"


def test_unrelated_malformed_parent_receipt_does_not_block_custom_installation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom-installation"
    root.mkdir()
    journal = tmp_path / ".inhere-box-directory-migration.json"
    journal.write_bytes(b"unrelated incomplete journal")
    require_installation_migrated(root)
    assert journal.read_bytes() == b"unrelated incomplete journal"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("driver", ["sqlite", "postgresql"])
def test_factory_checks_directory_before_pool_creation_or_first_run_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    source, target = _legacy(tmp_path)
    config = InhereBoxConfig(database=DatabaseConfig(driver=driver))

    def unexpected_pool(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pool must not be constructed")

    monkeypatch.setattr(factory, "SqlitePool", unexpected_pool)
    monkeypatch.setattr(factory, "PostgresPool", unexpected_pool)
    for operation in (factory.open_database, factory.should_defer_control_plane_db):
        with pytest.raises(BrandingMigrationError, match="--destination"):
            operation(config, PathLayout(target))
    assert (source / "octop.db").read_bytes() == b"preserve existing installation"
    assert not target.exists()


@pytest.mark.parametrize("explicit_old", [False, True])
def test_cli_init_refuses_old_installation_before_creating_any_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_old: bool
) -> None:
    source, target = _legacy(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("OCTOP_HOME", raising=False)
    if explicit_old:
        monkeypatch.setenv("INHERE_BOX_HOME", str(source))
    else:
        monkeypatch.delenv("INHERE_BOX_HOME", raising=False)
    result = CliRunner().invoke(
        cli,
        ["init", "--yes", "--admin-username", "admin", "--admin-password", "BootstrapPass123"],
    )
    assert result.exit_code == 1
    assert "--destination" in result.output
    assert not target.exists()
    assert list(source.iterdir()) == [source / "octop.db"]


async def test_server_refuses_before_acquiring_daemon_lock_or_creating_home(tmp_path: Path) -> None:
    from inhere_box.infra.server import InhereBoxServer

    source, target = _legacy(tmp_path)
    guard = SimpleNamespace(acquire=AsyncMock(), close=AsyncMock())
    server = InhereBoxServer(target, daemon_bootstrap=guard)  # type: ignore[arg-type]
    with pytest.raises(BrandingMigrationError, match="--destination"):
        await server.start()
    guard.acquire.assert_not_awaited()
    assert not target.exists()
    assert list(source.iterdir()) == [source / "octop.db"]


async def test_launch_refuses_before_constructing_server_or_daemon_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inhere_box import launch

    source, target = _legacy(tmp_path)
    monkeypatch.setenv("INHERE_BOX_HOME", str(target))

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("runtime must not be constructed")

    monkeypatch.setattr(launch, "DaemonBootstrap", unexpected)
    monkeypatch.setattr(launch, "InhereBoxServer", unexpected)
    with pytest.raises(BrandingMigrationError, match="--destination"):
        await launch.run_foreground(
            host=None,
            port=None,
            reload=False,
            workers=1,
            log_level=None,
            ssl_certfile=None,
            ssl_keyfile=None,
        )
    assert not target.exists()
    assert list(source.iterdir()) == [source / "octop.db"]
