"""Installers must stop before changing an installation that needs migration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "1" * 32
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX shell installers")


def _script(name: str, entrypoint: str) -> str:
    source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    if entrypoint == "installer":
        # Exercise the real preflight without allowing package-manager/network actions.
        return source.split("# ── 操作系统检查", 1)[0]
    return source.split("<< 'WRAPPER'\n", 1)[1].split("\nWRAPPER", 1)[0]


def _environment(tmp_path: Path, home_variable: str | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("INHERE_BOX_", "OCTOP_"))
    }
    env["HOME"] = str(tmp_path)
    if home_variable:
        env[home_variable] = str(tmp_path / ".octop")
    return env


@posix_only
@pytest.mark.parametrize("name", ["install.sh", "install-octop.sh"])
@pytest.mark.parametrize("entrypoint", ["installer", "wrapper"])
@pytest.mark.parametrize("home_variable", [None, "INHERE_BOX_HOME", "OCTOP_HOME"])
def test_legacy_root_blocks_install_and_launch_before_mutation(
    tmp_path: Path, name: str, entrypoint: str, home_variable: str | None
) -> None:
    legacy = tmp_path / ".octop"
    legacy.mkdir()
    database = legacy / "octop.db"
    database.write_bytes(b"existing database")

    result = subprocess.run(
        ["bash", "-c", _script(name, entrypoint)],
        env=_environment(tmp_path, home_variable),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--destination" in result.stderr
    assert "--apply" in result.stderr
    assert database.read_bytes() == b"existing database"
    assert not (tmp_path / ".inhere-box").exists()


@posix_only
@pytest.mark.parametrize("name", ["install.sh", "install-octop.sh"])
@pytest.mark.parametrize("entrypoint", ["installer", "wrapper"])
def test_unfinished_migration_blocks_install_and_launch(
    tmp_path: Path, name: str, entrypoint: str
) -> None:
    journal = tmp_path / ".inhere-box-directory-migration.json"
    journal.write_text(json.dumps({"phase": "published"}), encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", _script(name, entrypoint)],
        env=_environment(tmp_path, None),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--destination" in result.stderr
    assert not (tmp_path / ".inhere-box").exists()


@posix_only
@pytest.mark.parametrize("name", ["install.sh", "install-octop.sh"])
@pytest.mark.parametrize("entrypoint", ["installer", "wrapper"])
@pytest.mark.parametrize("database_present", [False, True])
def test_completed_migration_allows_current_installation(
    tmp_path: Path, name: str, entrypoint: str, database_present: bool
) -> None:
    current = tmp_path / ".inhere-box"
    executable = current / "venv" / "bin" / "inhere-box"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$INHERE_BOX_HOME"\n', encoding="utf-8")
    executable.chmod(0o755)
    (current / ".inhere-box-directory-migration-owner").write_text(TOKEN, encoding="ascii")
    if database_present:
        (current / "inhere-box.db").write_bytes(b"existing database")
    journal = tmp_path / ".inhere-box-directory-migration.json"
    journal.write_text(
        json.dumps({"phase": "complete", "token": TOKEN, "database_present": database_present}),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", "-c", _script(name, entrypoint)],
        env=_environment(tmp_path, None),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if entrypoint == "wrapper":
        assert result.stdout.strip() == str(current)
    assert not (tmp_path / ".octop").exists()


@posix_only
@pytest.mark.parametrize("name", ["install.sh", "install-octop.sh"])
@pytest.mark.parametrize("entrypoint", ["installer", "wrapper"])
@pytest.mark.parametrize(
    "missing",
    [
        "root",
        "database",
        "owner",
        "mismatched-owner",
        "root-symlink",
        "owner-symlink",
        "database-symlink",
    ],
)
def test_completed_migration_requires_its_published_data(
    tmp_path: Path, name: str, entrypoint: str, missing: str
) -> None:
    current = tmp_path / ".inhere-box"
    if missing != "root":
        current.mkdir()
        if missing != "database":
            (current / "inhere-box.db").write_bytes(b"existing database")
        if missing != "owner":
            owner = "2" * 32 if missing == "mismatched-owner" else TOKEN
            (current / ".inhere-box-directory-migration-owner").write_text(owner, encoding="ascii")
    if missing == "root-symlink":
        actual = tmp_path / "other-installation"
        current.rename(actual)
        current.symlink_to(actual, target_is_directory=True)
    elif missing in {"owner-symlink", "database-symlink"}:
        filename = (
            ".inhere-box-directory-migration-owner"
            if missing == "owner-symlink"
            else "inhere-box.db"
        )
        original = current / filename
        actual = tmp_path / filename
        original.rename(actual)
        original.symlink_to(actual)
    journal = tmp_path / ".inhere-box-directory-migration.json"
    journal.write_text(
        json.dumps({"phase": "complete", "token": TOKEN, "database_present": True}),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = subprocess.run(
        ["bash", "-c", _script(name, entrypoint)],
        env=_environment(tmp_path, None),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--destination" in result.stderr
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    if missing == "root":
        assert not current.exists()
