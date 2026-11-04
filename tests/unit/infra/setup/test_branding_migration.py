"""Offline filename migration preserves SQLite WAL, identity and recoverability."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from inhere_box.infra.agentcomputer.bootstrap import DaemonBootstrap
from inhere_box.infra.agents.resident_worker_lock import ResidentWorkerLock
from inhere_box.infra.setup import branding_migration as migration


@pytest.fixture(autouse=True)
def clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("INHERE_BOX_DATABASE_SQLITE_PATH", "OCTOP_DATABASE_SQLITE_PATH"):
        monkeypatch.delenv(key, raising=False)


def _database(root: Path, *, configured: str | None = "octop.db") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "octop.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO messages(body) VALUES('octop.db stays in business text')")
    if configured is not None:
        (root / "config.json").write_text(
            json.dumps({"database": {"driver": "sqlite", "sqlite_path": configured}, "port": 8088}),
            encoding="utf-8",
        )
    identity = root / "agentcomputer-control" / "identity.json"
    identity.parent.mkdir()
    identity.write_bytes(b'{"boxID":"unchanged-installation-identity","restoreEpoch":7}')
    return path


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _rows(path: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM messages ORDER BY id").fetchall()


def test_preview_is_read_only_and_does_not_create_missing_root(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    assert migration.migrate_installation(absent, apply=True)["status"] == "current"
    assert not absent.exists()
    source = _database(tmp_path / "install")
    before = _files(source.parent)

    result = migration.migrate_installation(source.parent)

    assert result["status"] == "needs_migration"
    assert result["config_changed"] is True
    assert _files(source.parent) == before


@pytest.mark.parametrize("absolute", [False, True])
def test_apply_preserves_data_identity_and_backups_and_is_repeatable(
    tmp_path: Path, absolute: bool
) -> None:
    root = tmp_path / "installation"
    source = _database(root, configured=str(root / "octop.db") if absolute else "octop.db")
    original = _files(root)
    rows = _rows(source)

    result = migration.migrate_installation(root, apply=True)

    assert result["status"] == "migrated"
    assert not source.exists()
    assert _rows(root / "inhere-box.db") == rows
    assert (root / "branding-migration-backup" / "octop.db").read_bytes() == original["octop.db"]
    assert (root / "branding-migration-backup" / "config.json").read_bytes() == original[
        "config.json"
    ]
    assert (root / "agentcomputer-control" / "identity.json").read_bytes() == original[
        "agentcomputer-control/identity.json"
    ]
    config = json.loads((root / "config.json").read_bytes())
    expected = str(root / "inhere-box.db") if absolute else "inhere-box.db"
    assert config["database"]["sqlite_path"] == expected
    assert config["port"] == 8088
    assert not (root / "box-auth").exists()
    after = _files(root)
    assert migration.migrate_installation(root, apply=True)["status"] == "current"
    assert _files(root) == after
    migration.require_installation_migrated(root, root / "inhere-box.db")


def test_wal_snapshot_includes_committed_rows_and_preview_creates_no_sidecars(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wal-install"
    root.mkdir()
    source = root / "octop.db"
    # An abruptly exited writer leaves committed records in WAL without an
    # active process, reproducing a stopped/crashed installation honestly.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)'); "
            "c.execute(\"INSERT INTO messages(body) VALUES('committed in WAL')\"); "
            "c.commit(); os._exit(0)",
            str(source),
        ],
        check=True,
    )
    assert (root / "octop.db-wal").is_file()
    (root / "octop.db-shm").unlink(missing_ok=True)
    before = _files(root)
    migration.migrate_installation(root)
    assert _files(root) == before

    migration.migrate_installation(root, apply=True)

    assert _rows(root / "inhere-box.db") == [(1, "committed in WAL")]
    assert not (root / "octop.db-wal").exists()
    assert (root / "branding-migration-backup" / "octop.db-wal").is_file()
    assert not (root / "inhere-box.db-wal").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_independent_database_conflict_never_changes_either_file(
    tmp_path: Path, apply: bool
) -> None:
    source = _database(tmp_path / "install")
    (source.parent / "inhere-box.db").write_bytes(b"independent destination")
    before = _files(source.parent)
    with pytest.raises(migration.BrandingMigrationError, match="Both octop.db"):
        migration.migrate_installation(source.parent, apply=apply)
    assert _files(source.parent) == before


@pytest.mark.parametrize("writer", ["daemon", "resident", "box"])
@pytest.mark.parametrize("apply", [False, True])
def test_live_lifetime_locks_refuse_migration_without_changes(
    tmp_path: Path, writer: str, apply: bool
) -> None:
    source = _database(tmp_path / "install")
    if writer == "daemon":
        controller = DaemonBootstrap(source.parent / "agentcomputer-control")
        controller.acquire_sync()
        release = controller.close_sync
    else:
        path = source.parent / (
            "box-auth/daemon.lock" if writer == "box" else "resident-worker.lock"
        )
        lock = ResidentWorkerLock(path)
        lock.acquire()
        release = lock.release
    before = _files(source.parent)
    try:
        with pytest.raises(migration.BrandingMigrationError, match="active|offline"):
            migration.migrate_installation(source.parent, apply=apply)
        assert _files(source.parent) == before
    finally:
        release()


@pytest.mark.parametrize("failure", ["publish", "archive", "config", "complete"])
def test_interrupted_migration_recovers_only_its_verified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "install"
    source = _database(root)
    expected = _rows(source)
    real_link, real_replace, real_save = (
        migration.os.link,
        migration.os.replace,
        migration._save_state,
    )

    def interrupted_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if (failure == "publish" and Path(dst).name == "inhere-box.db") or (
            failure == "archive" and Path(dst).name == "octop.db"
        ):
            raise OSError("simulated interruption")
        real_link(src, dst, *args, **kwargs)

    def interrupted_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if failure == "config" and Path(dst).name == "config.json":
            raise OSError("simulated interruption")
        real_replace(src, dst, *args, **kwargs)

    def interrupted_save(path: Path, state: dict[str, Any]) -> None:
        if failure == "complete" and state["phase"] == "complete":
            raise OSError("simulated interruption")
        real_save(path, state)

    with monkeypatch.context() as patch:
        patch.setattr(migration.os, "link", interrupted_link)
        patch.setattr(migration.os, "replace", interrupted_replace)
        patch.setattr(migration, "_save_state", interrupted_save)
        with pytest.raises(OSError, match="simulated interruption"):
            migration.migrate_installation(root, apply=True)
    with pytest.raises(migration.BrandingMigrationError, match="--apply"):
        migration.require_installation_migrated(root)
    before = _files(root)
    assert migration.migrate_installation(root)["recovered"] is True
    assert _files(root) == before
    assert migration.migrate_installation(root, apply=True)["recovered"] is True
    assert _rows(root / "inhere-box.db") == expected
    assert not source.exists()


def test_pending_state_does_not_authorize_overwriting_a_different_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    _database(root)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_resume", lambda *_: None)
        migration.migrate_installation(root, apply=True)
    (root / "inhere-box.db").write_bytes(b"independent conflicting database")
    before = _files(root)
    with pytest.raises(migration.BrandingMigrationError, match="conflicts"):
        migration.migrate_installation(root, apply=True)
    assert _files(root) == before


def test_published_journal_survives_a_later_prepare_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    _database(root)
    real_save = migration._save_state

    def interrupted_save(path: Path, state: dict[str, Any]) -> None:
        real_save(path, state)
        raise OSError("journal published before interruption")

    with monkeypatch.context() as patch:
        patch.setattr(migration, "_save_state", interrupted_save)
        with pytest.raises(OSError, match="journal published"):
            migration.migrate_installation(root, apply=True)
    assert migration.migrate_installation(root, apply=True)["recovered"] is True
    assert _rows(root / "inhere-box.db") == [(1, "octop.db stays in business text")]


def test_external_legacy_config_is_rejected_without_touching_external_root(tmp_path: Path) -> None:
    outside = _database(tmp_path / "outside")
    root = tmp_path / "install"
    _database(root, configured=str(outside))
    before = _files(tmp_path)
    with pytest.raises(migration.BrandingMigrationError, match="separate, explicitly scoped"):
        migration.migrate_installation(root, apply=True)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("source", ["process", "env", ".env"])
@pytest.mark.parametrize("prefix", ["INHERE_BOX", "OCTOP"])
def test_legacy_environment_reference_requires_explicit_startup_config_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, prefix: str
) -> None:
    root = tmp_path / "install"
    _database(root)
    key = prefix + "_DATABASE_SQLITE_PATH"
    if source == "process":
        monkeypatch.setenv(key, str(tmp_path / "outside" / "octop.db"))
    else:
        (root / source).write_text(f'{key}="octop.db"\n', encoding="utf-8")
    before = _files(root)
    with pytest.raises(migration.BrandingMigrationError, match="Update .*SQLITE_PATH"):
        migration.migrate_installation(root, apply=True)
    assert _files(root) == before


def test_runtime_gate_is_read_only_and_rejects_explicit_legacy_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(migration.BrandingMigrationError, match="--apply"):
        migration.require_installation_migrated(root, tmp_path / "external" / "octop.db")
    assert list(root.iterdir()) == []


def test_runtime_gate_refuses_missing_completed_database_or_invalid_journal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _database(root)
    migration.migrate_installation(root, apply=True)
    (root / "inhere-box.db").unlink()
    with pytest.raises(migration.BrandingMigrationError, match="--apply"):
        migration.require_installation_migrated(root)
    (root / ".inhere-box-branding-migration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(migration.BrandingMigrationError, match="--apply"):
        migration.require_installation_migrated(root)


def test_corrupt_sqlite_is_never_published(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = _database(root)
    source.write_bytes(b"SQLite format 3\0" + b"invalid database" * 100)
    original = source.read_bytes()
    config = (root / "config.json").read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        migration.migrate_installation(root, apply=True)
    assert source.read_bytes() == original
    assert (root / "config.json").read_bytes() == config
    assert not (root / "inhere-box.db").exists()
    assert not (root / ".inhere-box-branding-migration.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_symlinked_database_or_lock_directory_cannot_escape_the_root(tmp_path: Path) -> None:
    outside = _database(tmp_path / "outside")
    root = tmp_path / "install"
    root.mkdir()
    (root / "octop.db").symlink_to(outside)
    before = _files(tmp_path / "outside")
    with pytest.raises(migration.BrandingMigrationError, match="regular file"):
        migration.migrate_installation(root, apply=True)
    assert _files(tmp_path / "outside") == before
    (root / "octop.db").unlink()
    root.rmdir()
    _database(root)
    (root / "agentcomputer-control" / "identity.json").unlink()
    (root / "agentcomputer-control").rmdir()
    (root / "agentcomputer-control").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(migration.BrandingMigrationError, match="lock directory"):
        migration.migrate_installation(root, apply=True)
    assert _files(tmp_path / "outside") == before


def test_module_cli_previews_and_reports_conflicts_as_json(tmp_path: Path) -> None:
    root = tmp_path / "install"
    _database(root)
    command = [sys.executable, "-m", migration.__name__, "--root", str(root)]
    preview = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(preview.stdout)["status"] == "needs_migration"
    assert not (root / ".inhere-box-branding-migration.json").exists()
    (root / "inhere-box.db").touch()
    conflict = subprocess.run(command + ["--apply"], capture_output=True, text=True, check=False)
    assert conflict.returncode == 1
    assert json.loads(conflict.stderr)["status"] == "error"


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("apply", [False, True])
def test_target_sidecars_are_never_adopted_or_deleted(
    tmp_path: Path, sidecar: str, apply: bool
) -> None:
    root = tmp_path / "install"
    _database(root)
    (root / ("inhere-box.db" + sidecar)).write_bytes(b"belongs to another database")
    before = _files(root)
    with pytest.raises(migration.BrandingMigrationError, match="target SQLite sidecar"):
        migration.migrate_installation(root, apply=apply)
    assert _files(root) == before


def test_real_unrelated_target_wal_cannot_overwrite_migrated_rows(tmp_path: Path) -> None:
    root = tmp_path / "install"
    source = _database(root)
    target = root / "inhere-box.db"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)'); "
            "c.execute(\"INSERT INTO messages(body) VALUES('UNRELATED')\"); "
            "c.commit(); os._exit(0)",
            str(target),
        ],
        check=True,
    )
    target.unlink()
    before = _files(root)
    with pytest.raises(migration.BrandingMigrationError, match="target SQLite sidecar"):
        migration.migrate_installation(root, apply=True)
    assert _files(root) == before
    assert _rows(source) == [(1, "octop.db stays in business text")]
    assert not target.exists()


def test_orphaned_target_wal_also_blocks_fresh_initialization(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "inhere-box.db-wal").write_bytes(b"orphan WAL")
    before = _files(root)
    with pytest.raises(migration.BrandingMigrationError, match="target SQLite sidecar"):
        migration.migrate_installation(root, apply=True)
    assert _files(root) == before


def test_target_sidecar_rechecked_after_obtaining_offline_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    source = _database(root)
    before = source.read_bytes()
    offline = migration._offline

    @contextmanager
    def sidecar_appeared(path: Path) -> Iterator[None]:
        with offline(path):
            (path / "inhere-box.db-wal").write_bytes(b"new conflict after preflight")
            yield

    monkeypatch.setattr(migration, "_offline", sidecar_appeared)
    with pytest.raises(migration.BrandingMigrationError, match="target SQLite sidecar"):
        migration.migrate_installation(root, apply=True)
    assert source.read_bytes() == before
    assert not (root / "inhere-box.db").exists()
    assert (root / "inhere-box.db-wal").read_bytes() == b"new conflict after preflight"


@pytest.mark.parametrize("published", [False, True])
def test_pending_recovery_refuses_target_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    root = tmp_path / "install"
    _database(root)
    resume = migration._resume

    def pause(path: Path, state: dict[str, Any]) -> None:
        if published:
            os.link(path / state["staged_database"], path / "inhere-box.db")

    with monkeypatch.context() as patch:
        patch.setattr(migration, "_resume", pause)
        migration.migrate_installation(root, apply=True)
    assert migration._resume is resume
    (root / "inhere-box.db-shm").write_bytes(b"not part of this migration")
    before = _files(root)
    for apply in (False, True):
        with pytest.raises(migration.BrandingMigrationError, match="target SQLite sidecar"):
            migration.migrate_installation(root, apply=apply)
        assert _files(root) == before


@pytest.mark.parametrize("apply", [False, True])
def test_destination_delegates_to_directory_migration_without_filename_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    root, destination = tmp_path / ".octop", tmp_path / ".inhere-box"
    calls: list[tuple[Path, Path, bool]] = []
    expected = {"status": "directory preview", "destination": str(destination)}
    module = ModuleType("inhere_box.infra.setup.branding_directory_migration")

    def delegated(source: Path, target: Path, *, apply: bool = False) -> dict[str, Any]:
        calls.append((source, target, apply))
        return expected

    monkeypatch.setattr(module, "migrate_directory", delegated, raising=False)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert migration.migrate_installation(root, destination=destination, apply=apply) == expected
    args = ["--root", str(root), "--destination", str(destination)]
    assert migration.main(args + (["--apply"] if apply else [])) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert calls == [(root, destination, apply), (root, destination, apply)]
    assert not root.exists() and not destination.exists()
