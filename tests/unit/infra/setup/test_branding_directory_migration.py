"""Directory migration preserves originals and resumes only its own staged data."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from inhere_box.infra.agents.resident_worker_lock import ResidentWorkerLock
from inhere_box.infra.setup import branding_directory_migration as migration
from inhere_box.infra.setup.branding_migration import (
    BrandingMigrationError,
    migrate_installation,
    require_installation_migrated,
)


@pytest.fixture(autouse=True)
def clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("INHERE_BOX_DATABASE_SQLITE_PATH", "OCTOP_DATABASE_SQLITE_PATH"):
        monkeypatch.delenv(key, raising=False)


def _installation(parent: Path) -> tuple[Path, Path]:
    old, new = parent / ".octop", parent / ".inhere-box"
    old.mkdir()
    with sqlite3.connect(old / "octop.db") as conn:
        conn.execute("CREATE TABLE messages(body TEXT)")
        conn.execute("INSERT INTO messages VALUES (?)", (f"keep business text {old}",))
        conn.execute(
            "CREATE TABLE projects(project_id TEXT, directory TEXT, client_directory TEXT)"
        )
        conn.execute(
            "INSERT INTO projects VALUES ('p1', ?, ?)",
            (str(old / "projects" / "p1"), str(old / "client")),
        )
    (old / "config.json").write_text(
        json.dumps({"database": {"sqlite_path": str(old / "octop.db")}, "port": 8088}),
        encoding="utf-8",
    )
    workspace = old / "agents" / "a1" / ".octop"
    workspace.mkdir(parents=True)
    (workspace / "checkpoint").write_bytes(b"SDK data remains unchanged")
    identity = old / "box-auth" / "identity.json"
    identity.parent.mkdir()
    identity.write_bytes(b'{"box_id":"stable-box-identity"}')
    return old, new


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def _run(old: Path, new: Path) -> dict[str, Any]:
    return migrate_installation(old, destination=new, apply=True)


def _pause(*_args: Any) -> None:
    raise OSError("pause")


def test_preview_does_not_write_or_create_destination(tmp_path: Path) -> None:
    old, new = _installation(tmp_path)
    before = _files(tmp_path)
    result = migrate_installation(old, destination=new)
    assert result["status"] == "needs_migration"
    assert not new.exists()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("apply", [False, True])
def test_ready_preflight_refuses_modified_staging_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    old, new = _installation(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_publish", _pause)
        with pytest.raises(OSError, match="pause"):
            _run(old, new)
    state = json.loads((tmp_path / ".inhere-box-directory-migration.json").read_bytes())
    stage = tmp_path / state["staging"]
    (stage / "config.json").write_bytes(b"independent staging change")
    before = _files(tmp_path)
    with pytest.raises(BrandingMigrationError, match="Staged installation changed"):
        migrate_installation(old, destination=new, apply=apply)
    assert _files(tmp_path) == before
    assert not new.exists()


def test_ready_preview_refuses_active_original_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _installation(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_publish", _pause)
        with pytest.raises(OSError, match="pause"):
            _run(old, new)
    lock = ResidentWorkerLock(old / "resident-worker.lock")
    lock.acquire()
    before = _files(tmp_path)
    try:
        for apply in (False, True):
            with pytest.raises(BrandingMigrationError, match="active"):
                migrate_installation(old, destination=new, apply=apply)
            assert _files(tmp_path) == before
            assert not new.exists()
    finally:
        lock.release()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_published_recovery_rejects_replaced_source_symlink_before_reading_external_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _installation(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_finish", _pause)
        with pytest.raises(OSError, match="pause"):
            _run(old, new)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"external data")
    old.rename(tmp_path / "displaced-original")
    old.symlink_to(outside, target_is_directory=True)
    digest = migration._digest

    def guarded_digest(root: Path) -> str:
        assert root.resolve() != outside, "external tree must never be read"
        return digest(root)

    monkeypatch.setattr(migration, "_digest", guarded_digest)
    before = _files(tmp_path)
    for apply in (False, True):
        with pytest.raises(BrandingMigrationError, match="not a real directory"):
            migrate_installation(old, destination=new, apply=apply)
        assert _files(tmp_path) == before


def test_root_database_paths_and_internal_sdk_directory_migrate(tmp_path: Path) -> None:
    old, new = _installation(tmp_path)
    original = _files(old)
    result = _run(old, new)
    backup = Path(result["backup"])
    assert result["status"] == "migrated"
    assert not old.exists()
    assert (new / "inhere-box.db").is_file()
    assert not (new / "octop.db").exists()
    with sqlite3.connect(new / "inhere-box.db") as conn:
        assert conn.execute("SELECT body FROM messages").fetchone() == (
            f"keep business text {old}",
        )
        assert conn.execute("SELECT directory, client_directory FROM projects").fetchone() == (
            str(new / "projects" / "p1"),
            str(old / "client"),
        )
    assert json.loads((new / "config.json").read_bytes())["database"]["sqlite_path"] == str(
        new / "inhere-box.db"
    )
    assert (new / "agents" / "a1" / ".octop" / "checkpoint").read_bytes() == original[
        "agents/a1/.octop/checkpoint"
    ]
    assert (new / "box-auth" / "identity.json").read_bytes() == original["box-auth/identity.json"]
    for name, content in original.items():
        assert (backup / name).read_bytes() == content
    # Normal business writes after completion must not invalidate the receipt.
    (new / "later-file").write_text("new business data")
    assert _run(old, new)["status"] == "current"


@pytest.mark.parametrize("already_new_filename", [False, True])
def test_completed_receipt_prevents_reinitialization_after_losing_database(
    tmp_path: Path, already_new_filename: bool
) -> None:
    old, new = _installation(tmp_path)
    if already_new_filename:
        (old / "octop.db").rename(old / "inhere-box.db")
        (old / "config.json").write_text(
            json.dumps({"database": {"sqlite_path": str(old / "inhere-box.db")}}),
            encoding="utf-8",
        )
    _run(old, new)
    require_installation_migrated(new)
    (new / "inhere-box.db").unlink()
    with pytest.raises(BrandingMigrationError):
        require_installation_migrated(new)
    with pytest.raises(BrandingMigrationError, match="database is missing"):
        _run(old, new)
    assert not (new / "inhere-box.db").exists()


def test_uninitialized_root_can_initialize_after_directory_migration(tmp_path: Path) -> None:
    old, new = tmp_path / ".octop", tmp_path / ".inhere-box"
    old.mkdir()
    (old / "env").write_text("INHERE_BOX_PORT=8088\n", encoding="utf-8")
    _run(old, new)
    require_installation_migrated(new)
    assert not (new / "inhere-box.db").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_existing_roots_are_never_combined_or_overwritten(tmp_path: Path, apply: bool) -> None:
    old, new = _installation(tmp_path)
    new.mkdir()
    (new / "independent").write_bytes(b"other installation")
    before = _files(tmp_path)
    with pytest.raises(BrandingMigrationError, match="Both installation roots"):
        migrate_installation(old, destination=new, apply=apply)
    assert _files(tmp_path) == before


def test_failure_in_copy_processing_preserves_source_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _installation(tmp_path)
    original = _files(old)
    rewrite = migration.rewrite_installation_paths

    def fail(*_args: Any) -> dict[str, int]:
        raise BrandingMigrationError("simulated invalid configuration")

    monkeypatch.setattr(migration, "rewrite_installation_paths", fail)
    with pytest.raises(BrandingMigrationError, match="simulated"):
        _run(old, new)
    assert not new.exists()
    for name, content in original.items():
        assert (old / name).read_bytes() == content
    monkeypatch.setattr(migration, "rewrite_installation_paths", rewrite)
    assert _run(old, new)["recovered"] is True


@pytest.mark.parametrize("phase", ["ready", "published"])
def test_recovery_after_publication_before_journal_or_archival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    old, new = _installation(tmp_path)
    save = migration._save_state

    def interrupt(parent: Path, state: dict[str, Any]) -> None:
        # 'ready' is already durable before stage->destination rename.
        if state["phase"] == "published" and phase == "ready":
            raise OSError("simulated crash after publication")
        if state["phase"] == "complete" and phase == "published":
            raise OSError("simulated crash after archival")
        save(parent, state)

    monkeypatch.setattr(migration, "_save_state", interrupt)
    with pytest.raises(OSError, match="simulated crash"):
        _run(old, new)
    assert new.is_dir()
    monkeypatch.setattr(migration, "_save_state", save)
    assert _run(old, new)["status"] == "migrated"
    assert not old.exists()


def test_recovery_refuses_modified_published_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _installation(tmp_path)
    finish = migration._finish

    def interrupt(*_args: Any) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(migration, "_finish", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        _run(old, new)
    (new / "config.json").write_bytes(b"independent changes")
    monkeypatch.setattr(migration, "_finish", finish)
    with pytest.raises(BrandingMigrationError, match="Published installation changed"):
        _run(old, new)
    assert old.is_dir()


def test_active_installation_and_mountpoint_refuse_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _installation(tmp_path)
    lock = ResidentWorkerLock(old / "resident-worker.lock")
    lock.acquire()
    before = _files(tmp_path)
    try:
        with pytest.raises(BrandingMigrationError, match="active"):
            _run(old, new)
        assert _files(tmp_path) == before
    finally:
        lock.release()
    monkeypatch.setattr(migration.os.path, "ismount", lambda _path: True)
    with pytest.raises(BrandingMigrationError, match="mount point"):
        _run(old, new)
    assert not new.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink creation")
def test_internal_absolute_links_follow_new_root_and_external_links_stay(
    tmp_path: Path,
) -> None:
    old, new = _installation(tmp_path)
    (old / "internal-link").symlink_to(old / "agents", target_is_directory=True)
    external = tmp_path / "external"
    external.write_bytes(b"external content")
    (old / "external-link").symlink_to(external)
    _run(old, new)
    assert os.readlink(new / "internal-link") == str(new / "agents")
    assert os.readlink(new / "external-link") == str(external)
    assert external.read_bytes() == b"external content"
