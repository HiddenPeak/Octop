"""Offline installation-root migration, staged without changing the original data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from inhere_box.infra.agents.resident_worker_lock import ResidentWorkerLock
from inhere_box.infra.setup.branding_migration import (
    BrandingMigrationError,
    _offline,
    _preview_idle,
    _sync_directory,
    migrate_installation,
)
from inhere_box.infra.setup.branding_paths import BrandingPathsError, rewrite_installation_paths

_JOURNAL = ".inhere-box-directory-migration.json"
_OWNER = ".inhere-box-directory-migration-owner"


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _roots(source: Path, destination: Path) -> tuple[Path, Path]:
    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    if (
        source.name != ".octop"
        or destination.name != ".inhere-box"
        or source.parent.resolve() != destination.parent.resolve()
    ):
        raise BrandingMigrationError(
            "Directory migration requires sibling .octop and .inhere-box roots"
        )
    parent = source.parent.resolve()
    return parent / source.name, parent / destination.name


def _read_state(source: Path, destination: Path) -> dict[str, Any] | None:
    path = source.parent / _JOURNAL
    if not _exists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise BrandingMigrationError(f"Invalid directory migration journal: {path}")
    try:
        state = json.loads(path.read_bytes())
        if (
            not isinstance(state, dict)
            or set(state)
            != {
                "version",
                "source",
                "destination",
                "staging",
                "backup",
                "token",
                "phase",
                "source_digest",
                "target_digest",
                "database_present",
                "rewritten",
            }
            or state["version"] != 1
            or state["source"] != str(source)
            or state["destination"] != str(destination)
            or state["phase"] not in {"building", "ready", "published", "complete"}
            or not isinstance(state["token"], str)
            or len(state["token"]) != 32
            or any(c not in "0123456789abcdef" for c in state["token"])
            or state["staging"] != f".inhere-box.migrating-{state['token']}"
            or state["backup"] != f".octop.before-inhere-box-{state['token']}"
            or not isinstance(state["rewritten"], dict)
            or (
                not isinstance(state["database_present"], bool)
                and not (state["phase"] == "building" and state["database_present"] is None)
            )
        ):
            raise ValueError("invalid directory migration state")
        for key in ("source_digest", "target_digest"):
            value = state[key]
            if value is None and state["phase"] == "building":
                continue
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("invalid tree digest")
        return state
    except (ValueError, TypeError, KeyError) as exc:
        raise BrandingMigrationError(f"Invalid directory migration journal: {path}") from exc


def _save_state(parent: Path, state: dict[str, Any]) -> None:
    temporary = parent / f".{uuid.uuid4().hex}.journal.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, parent / _JOURNAL)
        _sync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(root: Path, *, sync: bool = False) -> str:
    """Hash names, file bytes and link targets without following any symlink."""
    result = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix().encode("utf-8")
            mode = path.lstat().st_mode
            result.update(len(relative).to_bytes(8, "big") + relative)
            if stat.S_ISLNK(mode):
                result.update(b"L" + os.fsencode(os.readlink(path)) + b"\0")
            elif stat.S_ISDIR(mode):
                result.update(b"D")
            elif stat.S_ISREG(mode):
                result.update(b"F" + path.stat().st_size.to_bytes(8, "big"))
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        result.update(chunk)
                    if sync:
                        if os.name == "nt":
                            # FlushFileBuffers needs a writable handle. Only the
                            # copied file is temporarily made writable, preserving
                            # readonly attributes from the original installation.
                            mode = stat.S_IMODE(path.stat().st_mode)
                            try:
                                path.chmod(mode | stat.S_IWRITE)
                                with path.open("r+b") as writable:
                                    os.fsync(writable.fileno())
                            finally:
                                path.chmod(mode)
                        else:
                            os.fsync(stream.fileno())
            else:
                raise BrandingMigrationError(f"Unsupported installation file: {path}")
    if sync:
        for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
            _sync_directory(Path(directory))
    return result.hexdigest()


def _owned(root: Path, token: str) -> None:
    marker = root / _OWNER
    if (
        root.is_symlink()
        or not root.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text("ascii") != token
    ):
        raise BrandingMigrationError(f"Directory does not belong to this migration: {root}")


def _validate_complete(destination: Path, state: dict[str, Any]) -> None:
    _owned(destination, state["token"])
    database = destination / "inhere-box.db"
    if state["database_present"] and (database.is_symlink() or not database.is_file()):
        raise BrandingMigrationError("The completed migration database is missing or replaced")


def _validate_source(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise BrandingMigrationError(f"Legacy installation root is not a real directory: {source}")
    if os.path.ismount(source):
        raise BrandingMigrationError(
            "Legacy root is a mount point; mount its parent or migrate it offline on the host"
        )
    if _exists(destination):
        raise BrandingMigrationError(
            f"Both installation roots exist; refusing to overwrite {destination}"
        )
    if _exists(source / _OWNER):
        raise BrandingMigrationError("Legacy root contains a conflicting migration marker")
    for name in ("octop.db", "inhere-box.db", "config.json", "env", ".env"):
        path = source / name
        if _exists(path) and (path.is_symlink() or not path.is_file()):
            raise BrandingMigrationError(f"Expected a regular installation file: {path}")
    if _exists(source / "octop.db") and _exists(source / "inhere-box.db"):
        raise BrandingMigrationError("Both octop.db and inhere-box.db exist; refusing to overwrite")
    _preview_idle(source)


@contextmanager
def _parent_lock(parent: Path) -> Iterator[None]:
    path = parent / ".inhere-box-directory-migration.lock"
    if path.is_symlink() or (_exists(path) and not path.is_file()):
        raise BrandingMigrationError(f"Invalid migration lock: {path}")
    lock = ResidentWorkerLock(path)
    try:
        lock.acquire()
    except RuntimeError as exc:
        raise BrandingMigrationError("Another directory migration is running") from exc
    try:
        yield
    finally:
        lock.release()


def _build(source: Path, destination: Path, state: dict[str, Any]) -> None:
    stage = source.parent / state["staging"]
    if _exists(stage):
        _owned(stage, state["token"])
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    (stage / _OWNER).write_text(state["token"], encoding="ascii")
    with _offline(source):
        shutil.copytree(source, stage, symlinks=True, dirs_exist_ok=True)
        state["source_digest"] = _digest(source)
        try:
            state["rewritten"] = rewrite_installation_paths(stage, source, destination)
        except BrandingPathsError as exc:
            raise BrandingMigrationError(str(exc)) from exc
        migrate_installation(stage, apply=True)
        state["database_present"] = (stage / "inhere-box.db").is_file()
        state["target_digest"] = _digest(stage, sync=True)
        state["phase"] = "ready"
        _save_state(source.parent, state)


def _validate_pending(source: Path, destination: Path, state: dict[str, Any]) -> None:
    """Read-only preflight for a staged or published directory migration."""
    stage = source.parent / state["staging"]
    backup = source.parent / state["backup"]
    if _exists(source):
        if source.is_symlink() or not source.is_dir() or os.path.ismount(source):
            raise BrandingMigrationError(
                f"Legacy installation root is not a real directory: {source}"
            )
        if _exists(backup):
            raise BrandingMigrationError(f"Conflicting installation backup: {backup}")
        _preview_idle(source)
        if _digest(source) != state["source_digest"]:
            raise BrandingMigrationError("Legacy installation changed during migration")
    elif backup.is_symlink() or not backup.is_dir() or _digest(backup) != state["source_digest"]:
        raise BrandingMigrationError("Original installation backup is missing or changed")
    if _exists(destination):
        if _exists(stage):
            raise BrandingMigrationError(f"Migration destination appeared: {destination}")
        candidate = destination
    else:
        if state["phase"] == "published":
            raise BrandingMigrationError("Published installation is missing")
        candidate = stage
    _owned(candidate, state["token"])
    if _digest(candidate) != state["target_digest"]:
        raise BrandingMigrationError(
            "Published installation changed before migration completed"
            if candidate == destination
            else "Staged installation changed; refusing to publish"
        )


def _publish(source: Path, destination: Path, state: dict[str, Any]) -> None:
    stage = source.parent / state["staging"]
    if _exists(destination):
        # Recovery after rename but before recording its phase. Ownership and
        # content must both match; never accept an independent destination.
        if _exists(stage):
            raise BrandingMigrationError(f"Migration destination appeared: {destination}")
        _owned(destination, state["token"])
        if _digest(destination) != state["target_digest"]:
            raise BrandingMigrationError(
                "Published installation changed before migration completed"
            )
    else:
        _owned(stage, state["token"])
        if _digest(stage) != state["target_digest"]:
            raise BrandingMigrationError("Staged installation changed; refusing to publish")
        stage.rename(destination)
        _sync_directory(source.parent)
    state["phase"] = "published"
    _save_state(source.parent, state)


def _finish(source: Path, destination: Path, state: dict[str, Any]) -> None:
    _owned(destination, state["token"])
    if _digest(destination) != state["target_digest"]:
        raise BrandingMigrationError("Published installation changed before migration completed")
    backup = source.parent / state["backup"]
    if _exists(source):
        if source.is_symlink() or not source.is_dir() or os.path.ismount(source):
            raise BrandingMigrationError(
                f"Legacy installation root is not a real directory: {source}"
            )
        if _exists(backup):
            raise BrandingMigrationError(f"Conflicting installation backup: {backup}")
        _preview_idle(source)
        if _digest(source) != state["source_digest"]:
            raise BrandingMigrationError("Legacy installation changed during migration")
        # Lifetime lock handles are closed before this rename for Windows.
        source.rename(backup)
        _sync_directory(source.parent)
    elif backup.is_symlink() or not backup.is_dir() or _digest(backup) != state["source_digest"]:
        raise BrandingMigrationError("Original installation backup is missing or changed")
    state["phase"] = "complete"
    _save_state(source.parent, state)


def migrate_directory(source: Path, destination: Path, *, apply: bool = False) -> dict[str, Any]:
    """Preview, apply or resume migration with a complete original-root backup."""
    source, destination = _roots(source, destination)
    state = _read_state(source, destination)
    result: dict[str, Any] = {
        "status": "needs_migration",
        "root": str(source),
        "destination": str(destination),
        "recovered": state is not None and state["phase"] != "complete",
    }
    if state is not None and state["phase"] == "complete":
        if _exists(source):
            raise BrandingMigrationError("Legacy root reappeared after a completed migration")
        _validate_complete(destination, state)
        result.update(status="current", backup=str(source.parent / state["backup"]))
        return result
    if state is None or state["phase"] == "building":
        _validate_source(source, destination)
    else:
        _validate_pending(source, destination, state)
    if not apply:
        if state is not None:
            result["backup"] = str(source.parent / state["backup"])
        return result
    with _parent_lock(source.parent):
        state = _read_state(source, destination)
        if state is None:
            _validate_source(source, destination)
            token = uuid.uuid4().hex
            state = {
                "version": 1,
                "source": str(source),
                "destination": str(destination),
                "staging": f".inhere-box.migrating-{token}",
                "backup": f".octop.before-inhere-box-{token}",
                "token": token,
                "phase": "building",
                "source_digest": None,
                "target_digest": None,
                "database_present": None,
                "rewritten": {},
            }
            _save_state(source.parent, state)
        if state["phase"] == "building":
            _validate_source(source, destination)
            _build(source, destination, state)
        if state["phase"] in {"ready", "published"}:
            _validate_pending(source, destination, state)
        if state["phase"] == "ready":
            _publish(source, destination, state)
        if state["phase"] == "published":
            _finish(source, destination, state)
        if state["phase"] != "complete":
            raise BrandingMigrationError("Directory migration did not complete")
        _validate_complete(destination, state)
    result.update(
        status="migrated",
        backup=str(source.parent / state["backup"]),
        rewritten=state["rewritten"],
    )
    return result
