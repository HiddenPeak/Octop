"""Explicit, offline database filename migration within one installation root.

The default operation is a read-only preview. Applying holds the daemon,
resident-worker and existing box-auth lifetime locks. Operators must stop any
older/external writers that do not participate in these lock contracts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from inhere_box.infra.agents.resident_worker_lock import ResidentWorkerLock
from inhere_box.infra.backup.restore_lock import offline_restore_guard
from inhere_box.infra.errors import InhereBoxError
from inhere_box.infra.utils.env_file import parse_env_text
from inhere_box.infra.utils.paths import PathLayout

_OLD = "octop.db"
_NEW = "inhere-box.db"
_STATE = ".inhere-box-branding-migration.json"
_DIRECTORY_STATE = ".inhere-box-directory-migration.json"
_BACKUP = "branding-migration-backup"
_SOURCE_FILES = (_OLD, _OLD + "-wal", _OLD + "-shm")


class BrandingMigrationError(ValueError):
    """Preflight or recovery failed without permission to discard either database."""


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BrandingMigrationError(f"Expected a regular file: {path}")


def _hint(root: Path) -> str:
    return (
        "Stop the installation, then run python -m "
        "inhere_box.infra.setup.branding_migration --root "
        f"{str(root)!r} --apply"
    )


def _read_state(root: Path) -> dict[str, Any] | None:
    path = root / _STATE
    if not _exists(path):
        return None
    _regular(path)
    try:
        value = json.loads(path.read_bytes())
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "phase",
                "source_files",
                "staged_database",
                "staged_config",
                "database_hash",
                "config_before_hash",
                "config_after_hash",
            }
            or value.get("version") != 1
            or value.get("phase") not in {"prepared", "complete"}
            or not isinstance(value.get("source_files"), dict)
            or set(value["source_files"]) - set(_SOURCE_FILES)
            or _OLD not in value["source_files"]
        ):
            raise ValueError("invalid migration state")
        for name in ("staged_database", "staged_config"):
            item = value.get(name)
            if item is None and name == "staged_config":
                continue
            if (
                not isinstance(item, str)
                or Path(item).name != item
                or not item.startswith(".branding-migration-")
            ):
                raise ValueError("invalid staging path")
        for item in [
            value.get("database_hash"),
            *value["source_files"].values(),
            value.get("config_before_hash"),
            value.get("config_after_hash"),
        ]:
            if item is not None and (
                not isinstance(item, str)
                or len(item) != 64
                or any(c not in "0123456789abcdef" for c in item)
            ):
                raise ValueError("invalid file digest")
        if value.get("database_hash") is None:
            raise ValueError("missing database digest")
        if (value["staged_config"] is None) != (value["config_after_hash"] is None) or (
            value["config_after_hash"] is not None and value["config_before_hash"] is None
        ):
            raise ValueError("incomplete config migration state")
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise BrandingMigrationError(f"Invalid migration state at {path}") from exc


def _directory_hint(source: Path, destination: Path) -> str:
    return (
        "Installation directory requires migration. Stop the installation, then run "
        "python -m inhere_box.infra.setup.branding_migration "
        f"--root {str(source)!r} --destination {str(destination)!r} --apply. "
        "Update the installation home setting to the destination before starting."
    )


def require_directory_migrated(root: Path) -> None:
    """Block legacy install roots and incomplete directory switches without writes.

    The name check applies to the selected installation root, never to SDK-owned
    .octop directories nested inside that installation's workspaces.
    """
    root = Path(os.path.abspath(root.expanduser()))
    source = root if root.name == ".octop" else root.with_name(".octop")
    destination = root.with_name(".inhere-box") if root.name == ".octop" else root
    journal = root.parent / _DIRECTORY_STATE
    if _exists(journal):
        matched = False
        try:
            _regular(journal)
            value = json.loads(journal.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("directory migration state must be an object")
            old, new = value.get("source"), value.get("destination")
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("directory migration paths are missing")
            old_path, new_path = Path(os.path.abspath(old)), Path(os.path.abspath(new))
            if root in {old_path, new_path}:
                matched = True
                source, destination = old_path, new_path
                if value.get("version") != 1 or value.get("phase") != "complete":
                    raise BrandingMigrationError(_directory_hint(source, destination))
                if root == destination:
                    owner = root / ".inhere-box-directory-migration-owner"
                    token = value.get("token")
                    database_present = value.get("database_present")
                    if (
                        root.is_symlink()
                        or not root.is_dir()
                        or owner.is_symlink()
                        or not owner.is_file()
                        or not isinstance(token, str)
                        or not token
                        or not token.isascii()
                        or owner.read_bytes() != token.encode("ascii")
                        or type(database_present) is not bool
                        or (
                            database_present
                            and ((root / _NEW).is_symlink() or not (root / _NEW).is_file())
                        )
                    ):
                        raise BrandingMigrationError(
                            "Completed directory migration destination is missing or changed. "
                            + _directory_hint(source, destination)
                        )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, BrandingMigrationError) and matched:
                raise
            if root.name in {".octop", ".inhere-box"}:
                raise BrandingMigrationError(
                    f"Invalid directory migration state at {journal}. "
                    + _directory_hint(source, destination)
                ) from exc
    if root.name == ".octop" or (root.name == ".inhere-box" and _exists(root.with_name(".octop"))):
        raise BrandingMigrationError(_directory_hint(source, destination))
    if root.is_symlink() and root.resolve().name == ".octop":
        legacy = root.resolve()
        raise BrandingMigrationError(_directory_hint(legacy, legacy.with_name(".inhere-box")))


def require_installation_migrated(root: Path, database_path: Path | None = None) -> None:
    """Refuse legacy filenames or incomplete migrations without opening SQLite."""
    require_directory_migrated(root)
    try:
        state = _read_state(root)
    except BrandingMigrationError as exc:
        raise BrandingMigrationError(f"{exc}. {_hint(root)}") from exc
    if (
        _exists(root / _OLD)
        or (database_path is not None and database_path.name == _OLD)
        or (state is not None and state["phase"] != "complete")
        or (state is not None and not _exists(root / _NEW))
    ):
        raise BrandingMigrationError(_hint(root))


def _hash(path: Path) -> str:
    _regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _config_update(root: Path) -> tuple[bytes | None, bytes | None]:
    path = root / "config.json"
    if not _exists(path):
        return None, None
    _regular(path)
    original = path.read_bytes()
    try:
        config = json.loads(original)
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        database = config.get("database", {})
        if not isinstance(database, dict):
            raise ValueError("config.database must be an object")
        if str(database.get("driver", "sqlite")).strip().lower() != "sqlite":
            if _exists(root / _OLD):
                raise ValueError("legacy SQLite file with a non-SQLite installation")
            return original, None
        configured = database.get("sqlite_path")
        if configured is None:
            return original, None
        if not isinstance(configured, str):
            raise ValueError("config.database.sqlite_path must be a string")
        selected = Path(configured)
        if selected.name != _OLD:
            return original, None
        absolute = Path(os.path.abspath(selected if selected.is_absolute() else root / selected))
        if absolute != root / _OLD:
            raise ValueError("SQLite path requires a separate, explicitly scoped migration")
        database["sqlite_path"] = str(root / _NEW) if selected.is_absolute() else _NEW
        updated = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
        return original, updated
    except (ValueError, TypeError) as exc:
        raise BrandingMigrationError(f"Cannot migrate {path}: {exc}") from exc


def _lock_paths(root: Path) -> tuple[Path, ...]:
    return (
        root / "agentcomputer-control" / "controller.lock",
        root / "resident-worker.lock",
        root / "box-auth" / "daemon.lock",
    )


def _check_environment(root: Path) -> None:
    sources = [("process environment", dict(os.environ))]
    for name in ("env", ".env"):
        path = root / name
        if _exists(path):
            _regular(path)
            sources.append((str(path), parse_env_text(path.read_text("utf-8"))))
    for source, values in sources:
        for key in ("INHERE_BOX_DATABASE_SQLITE_PATH", "OCTOP_DATABASE_SQLITE_PATH"):
            if Path(values.get(key, "").strip()).name == _OLD:
                raise BrandingMigrationError(
                    f"Update {key} in {source} to the new database filename before migration"
                )


def _check_lock_parents(root: Path) -> None:
    for name in ("agentcomputer-control", "box-auth"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise BrandingMigrationError(f"Invalid installation lock directory: {path}")


def _preview_idle(root: Path) -> None:
    # Reusing the OS primitives without O_CREAT keeps preview byte-for-byte
    # read-only, including Windows's normally initialized one-byte lock files.
    _check_lock_parents(root)
    with ExitStack() as cleanup:
        for path in _lock_paths(root):
            if not _exists(path):
                continue
            _regular(path)
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            cleanup.callback(os.close, fd)
            module: Any
            try:
                if os.name == "nt":
                    module = importlib.import_module("msvcrt")
                    module.locking(fd, module.LK_NBLCK, 1)
                    cleanup.callback(module.locking, fd, module.LK_UNLCK, 1)
                else:
                    module = importlib.import_module("fcntl")
                    module.flock(fd, module.LOCK_EX | module.LOCK_NB)
                    cleanup.callback(module.flock, fd, module.LOCK_UN)
            except OSError as exc:
                raise BrandingMigrationError(
                    f"Installation is active: {path}. {_hint(root)}"
                ) from exc


@contextmanager
def _offline(root: Path) -> Iterator[None]:
    _check_lock_parents(root)
    try:
        with offline_restore_guard(PathLayout(root)), ExitStack() as cleanup:
            # Creating box-auth on a server-mode installation would itself
            # change its account-mode fence. Only lock an existing box root.
            box = root / "box-auth"
            if box.exists():
                if box.is_symlink() or not box.is_dir():
                    raise BrandingMigrationError("Invalid box-auth directory")
                lock = ResidentWorkerLock(box / "daemon.lock")
                lock.acquire()
                cleanup.callback(lock.release)
            yield
    except (InhereBoxError, RuntimeError) as exc:
        raise BrandingMigrationError(f"Installation must be offline. {_hint(root)}") from exc


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _temporary(root: Path, suffix: str, content: bytes | None = None) -> Path:
    fd, name = tempfile.mkstemp(prefix=".branding-migration-", suffix=suffix, dir=root)
    path = Path(name)
    with os.fdopen(fd, "wb") as stream:
        if content is not None:
            stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _save_state(root: Path, state: dict[str, Any]) -> None:
    content = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()
    temporary = _temporary(root, ".json", content)
    try:
        os.replace(temporary, root / _STATE)
        _sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_config(root: Path, content: bytes) -> None:
    path = root / _BACKUP / "config.json"
    if _exists(path):
        if _hash(path) != hashlib.sha256(content).hexdigest():
            raise BrandingMigrationError(f"Conflicting config backup: {path}")
        return
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _prepare(root: Path, original: bytes | None, updated: bytes | None) -> dict[str, Any]:
    backup = root / _BACKUP
    if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
        raise BrandingMigrationError(f"Invalid backup directory: {backup}")
    backup.mkdir(mode=0o700, exist_ok=True)
    for name in _SOURCE_FILES:
        if _exists(backup / name):
            raise BrandingMigrationError(f"Conflicting database backup: {backup / name}")
    if original is not None and updated is not None:
        _backup_config(root, original)
    staged = _temporary(root, ".db")
    config_stage: Path | None = None
    state_saved = False
    try:

        def progress(status: int, _remaining: int, _total: int) -> None:
            if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise BrandingMigrationError("SQLite is busy; stop all installation writers")

        with (
            closing(sqlite3.connect((root / _OLD).as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(staged)) as target,
        ):
            source.backup(target, progress=progress)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BrandingMigrationError("SQLite snapshot failed integrity_check")
            # The published copy is self-contained; no WAL must be needed.
            target.execute("PRAGMA journal_mode=DELETE")
        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        if updated is not None:
            config_stage = _temporary(root, ".config", updated)
        state = {
            "version": 1,
            "phase": "prepared",
            "staged_database": staged.name,
            "staged_config": config_stage.name if config_stage else None,
            "source_files": {
                name: _hash(root / name) for name in _SOURCE_FILES if _exists(root / name)
            },
            "database_hash": _hash(staged),
            "config_before_hash": hashlib.sha256(original).hexdigest()
            if original is not None
            else None,
            "config_after_hash": hashlib.sha256(updated).hexdigest()
            if updated is not None
            else None,
        }
        _save_state(root, state)
        state_saved = True
        return state
    finally:
        # A failed directory fsync may occur after the journal was published.
        # Keep its snapshot whenever recovery can already observe that journal.
        if not state_saved and not _exists(root / _STATE):
            staged.unlink(missing_ok=True)
            if config_stage is not None:
                config_stage.unlink(missing_ok=True)


def _check_hash(path: Path, expected: str) -> None:
    if _hash(path) != expected:
        raise BrandingMigrationError(f"File changed or conflicts with migration state: {path}")


def _require_clean_target(root: Path) -> None:
    # A WAL or hot journal is part of its original database, even when that
    # database's main file disappeared. Never attach it to our new snapshot.
    for suffix in ("-wal", "-shm", "-journal"):
        path = root / (_NEW + suffix)
        if _exists(path):
            raise BrandingMigrationError(f"Conflicting target SQLite sidecar: {path}")


def _validate_pending(root: Path, state: dict[str, Any]) -> None:
    _require_clean_target(root)
    backup = root / _BACKUP
    if backup.is_symlink() or not backup.is_dir():
        raise BrandingMigrationError(f"Invalid migration backup directory: {backup}")
    for name in _SOURCE_FILES:
        present = [path for path in (root / name, backup / name) if _exists(path)]
        expected = state["source_files"].get(name)
        if (expected is None and present) or (expected is not None and not present):
            raise BrandingMigrationError(f"Unexpected migration source: {name}")
        for path in present:
            _check_hash(path, expected)
    target, staged = root / _NEW, root / state["staged_database"]
    if not _exists(target) and not _exists(staged):
        raise BrandingMigrationError("The verified migration snapshot is missing")
    for path in (target, staged):
        if _exists(path):
            _check_hash(path, state["database_hash"])
    config = root / "config.json"
    before, after = state["config_before_hash"], state["config_after_hash"]
    actual = _hash(config) if _exists(config) else None
    if actual not in {before, after} or (before is not None and actual is None):
        raise BrandingMigrationError("Config changed during database migration")
    if after is not None:
        _check_hash(backup / "config.json", before)
        stage = root / state["staged_config"]
        if actual != after or _exists(stage):
            _check_hash(stage, after)


def _resume(root: Path, state: dict[str, Any]) -> None:
    _validate_pending(root, state)
    target, staged = root / _NEW, root / state["staged_database"]
    if not _exists(target):
        # Hard-link publication is atomic and refuses an independently created
        # destination, unlike replace(). Both files are on this installation FS.
        os.link(staged, target)
        _sync_directory(root)
    staged.unlink(missing_ok=True)
    for name in _SOURCE_FILES:
        source, backup = root / name, root / _BACKUP / name
        if _exists(source):
            if not _exists(backup):
                os.link(source, backup)
                _sync_directory(backup.parent)
            source.unlink()
            _sync_directory(root)
    if state["config_after_hash"] is not None:
        config_stage = root / state["staged_config"]
        if _hash(root / "config.json") != state["config_after_hash"]:
            os.replace(config_stage, root / "config.json")
            _sync_directory(root)
        else:
            config_stage.unlink(missing_ok=True)
    state["phase"] = "complete"
    _save_state(root, state)


def migrate_installation(
    root: Path, *, destination: Path | None = None, apply: bool = False
) -> dict[str, Any]:
    """Preview or apply the filename migration, preserving root and business data."""
    if destination is not None:
        from inhere_box.infra.setup.branding_directory_migration import migrate_directory

        delegated: dict[str, Any] = migrate_directory(root, destination, apply=apply)
        return delegated
    root = root.expanduser().absolute()
    if root.exists():
        if not root.is_dir():
            raise BrandingMigrationError(f"Installation root is not a directory: {root}")
        root = root.resolve()
    result: dict[str, Any] = {
        "status": "current",
        "root": str(root),
        "database": {"source": str(root / _OLD), "target": str(root / _NEW)},
        "config_changed": False,
        "recovered": False,
    }
    if not root.exists():
        return result
    _check_environment(root)
    state = _read_state(root)
    pending = state is not None and state["phase"] == "prepared"
    source, target = root / _OLD, root / _NEW
    if not pending and _exists(source) and _exists(target):
        raise BrandingMigrationError("Both octop.db and inhere-box.db exist; refusing to overwrite")
    if state is not None and state["phase"] == "complete" and _exists(source):
        raise BrandingMigrationError("A legacy database reappeared after a completed migration")
    if state is not None and state["phase"] == "complete" and not _exists(target):
        raise BrandingMigrationError("The completed migration database is missing")
    if _exists(source) or not _exists(target):
        _require_clean_target(root)
    original: bytes | None = None
    updated: bytes | None = None
    if pending:
        assert state is not None
        _validate_pending(root, state)
        result.update(config_changed=state["config_after_hash"] is not None, recovered=True)
    else:
        original, updated = _config_update(root)
        if not _exists(source):
            if updated is not None:
                raise BrandingMigrationError("Configured legacy database is missing")
            return result
        _regular(source)
        with source.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\0":
                raise BrandingMigrationError("Legacy database is not a SQLite database")
        for name in _SOURCE_FILES:
            if _exists(root / name):
                _regular(root / name)
        result["config_changed"] = updated is not None
    _preview_idle(root)
    result["status"] = "needs_migration"
    result["database"]["backup"] = str(root / _BACKUP / _OLD)
    if not apply:
        return result
    with _offline(root):
        # Recheck after acquiring lifetime locks; preview granted no ownership.
        _require_clean_target(root)
        current = _read_state(root)
        if current is not None and current["phase"] == "prepared":
            state = current
        else:
            if _exists(target):
                raise BrandingMigrationError("Migration target appeared; refusing to overwrite")
            original, updated = _config_update(root)
            state = _prepare(root, original, updated)
        _resume(root, state)
    result["status"] = "migrated"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate_installation(args.root, destination=args.destination, apply=args.apply)
    except (BrandingMigrationError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
