"""FnOS launchers migrate installed databases before starting the application."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from inhere_box.infra.users.password import verify_password

posix_only = pytest.mark.skipif(os.name != "posix", reason="bash launchers")
REPO = Path(__file__).resolve().parents[2]
COMMON_SH = REPO / "scripts" / "fnos" / "common.sh"


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("INHERE_BOX_", "OCTOP_"))
        },
        "HOME": str(tmp_path / "data"),
        "INHERE_BOX_HOME": str(tmp_path / "data" / ".inhere-box"),
        "INHERE_BOX_ACCOUNT_MODE": "server",
        "INHERE_BOX_DEFAULT_PASSWORD": "FnosBootstrapPass123",
        "FNOS_TEST_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO / "src"),
    }


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _password_hash(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return str(
            connection.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[
                0
            ]
        )


def _native_launcher(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    app = tmp_path / "app"
    (app / "bin").mkdir(parents=True)
    (app / "cmd").mkdir()
    launcher = app / "bin" / "inhere-box"
    shutil.copy(REPO / "fnos" / "native" / "app" / "bin" / "inhere-box", launcher)
    # Use the test runtime instead of probing FnOS's Python 3.12 installation.
    (app / "cmd" / "common.sh").write_text(
        COMMON_SH.read_text(encoding="utf-8")
        + '\nfind_python312() { printf "%s" "$FNOS_TEST_PYTHON"; }\n',
        encoding="utf-8",
    )
    env = {
        **_environment(tmp_path),
        "TRIM_APPDEST": str(app),
        "TRIM_PKGVAR": str(tmp_path / "var"),
        "TRIM_DATA_SHARE_PATHS": str(tmp_path / "data"),
    }
    return launcher, env


def _legacy_database(home: Path) -> Path:
    database = home / "octop.db"
    (home / "inhere-box.db").rename(database)
    (home / "config.json").write_text(
        json.dumps({"database": {"driver": "sqlite", "sqlite_path": "octop.db"}}),
        encoding="utf-8",
    )
    return database


def _legacy_root(home: Path) -> Path:
    _legacy_database(home)
    legacy = home.with_name(".octop")
    home.rename(legacy)
    config = json.loads((legacy / "config.json").read_text(encoding="utf-8"))
    config["tls"] = {"cert_file": str(legacy / "tls" / "cert.pem")}
    (legacy / "config.json").write_text(json.dumps(config), encoding="utf-8")
    internal = legacy / "agents" / "test" / ".octop"
    internal.mkdir(parents=True)
    (internal / "keep.txt").write_text("harness data", encoding="utf-8")
    return legacy


@posix_only
@pytest.mark.parametrize(
    ("database_name", "move_root"),
    [("inhere-box.db", False), ("octop.db", False), ("octop.db", True)],
)
def test_native_prepare_migrates_and_preserves_initialized_database(
    tmp_path: Path, database_name: str, move_root: bool
) -> None:
    launcher, env = _native_launcher(tmp_path)
    _run(["bash", str(launcher), "--prepare"], env)
    home = Path(env["INHERE_BOX_HOME"])
    if move_root:
        database = _legacy_root(home) / database_name
    else:
        database = _legacy_database(home) if database_name == "octop.db" else home / database_name
    password_hash = _password_hash(database)

    repeated = _run(["bash", str(launcher), "--prepare"], env)

    assert "首次启动" not in repeated.stdout
    assert _password_hash(home / "inhere-box.db") == password_hash
    assert not (home / "octop.db").exists()
    if database_name == "octop.db":
        config = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert config["database"]["sqlite_path"] == "inhere-box.db"
    if move_root:
        assert not home.with_name(".octop").exists()
        assert config["tls"]["cert_file"] == str(home / "tls" / "cert.pem")
        assert (home / "agents/test/.octop/keep.txt").read_text("utf-8") == "harness data"
    files = set(home.rglob("*"))
    _run(["bash", str(launcher), "--prepare"], env)
    assert set(home.rglob("*")) == files
    assert _password_hash(home / "inhere-box.db") == password_hash


@posix_only
def test_native_prepare_conflicting_databases_prevent_init(tmp_path: Path) -> None:
    launcher, env = _native_launcher(tmp_path)
    _run(["bash", str(launcher), "--prepare"], env)
    home = Path(env["INHERE_BOX_HOME"])
    current = home / "inhere-box.db"
    legacy = home / "octop.db"
    shutil.copy2(current, legacy)
    with sqlite3.connect(legacy) as connection:
        connection.execute("UPDATE users SET display_name='Legacy database'")
    before = {path: path.read_bytes() for path in (current, legacy)}

    result = subprocess.run(
        ["bash", str(launcher), "--prepare"], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "首次启动" not in result.stdout
    assert {path: path.read_bytes() for path in before} == before


def _docker_bootstrap(tmp_path: Path, env: dict[str, str]) -> tuple[Path, Path]:
    _run(
        [
            "bash",
            "-c",
            'source "$1"; inhere_box_write_fnos_bootstrap "$2" admin "$INHERE_BOX_DEFAULT_PASSWORD"',
            "fnos-test",
            str(COMMON_SH),
            str(tmp_path / "data"),
        ],
        env,
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    command_log = tmp_path / "commands.log"
    wrapper = bindir / "inhere-box"
    wrapper.write_text(
        '#!/bin/bash\nset -euo pipefail\nprintf "%s\\n" "$1" >> "$FNOS_TEST_COMMAND_LOG"\n'
        'if [ "$1" = run ]; then exit 0; fi\n'
        'exec "$FNOS_TEST_PYTHON" -m inhere_box.cli.main "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    python = bindir / "python3"
    python.write_text('#!/bin/bash\nexec "$FNOS_TEST_PYTHON" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    env.update(
        PATH=str(bindir) + os.pathsep + env["PATH"],
        FNOS_TEST_COMMAND_LOG=str(command_log),
    )
    return tmp_path / "data" / "fnos-boot.sh", command_log


@posix_only
@pytest.mark.parametrize(
    ("database_name", "move_root"),
    [(None, False), ("inhere-box.db", False), ("octop.db", False), ("octop.db", True)],
)
def test_docker_bootstrap_starts_with_fresh_or_existing_database(
    tmp_path: Path, database_name: str | None, move_root: bool
) -> None:
    env = _environment(tmp_path)
    home = Path(env["INHERE_BOX_HOME"])
    database = home / (database_name or "inhere-box.db")
    password_hash = None
    if database_name is not None:
        _run(
            [
                sys.executable,
                "-m",
                "inhere_box.cli.main",
                "init",
                "--yes",
                "--admin-username",
                "admin",
                "--admin-password",
                env["INHERE_BOX_DEFAULT_PASSWORD"],
            ],
            env,
        )
        if move_root:
            database = _legacy_root(home) / database_name
        elif database_name == "octop.db":
            _legacy_database(home)
        password_hash = _password_hash(database)
        (database.parent / ".fnos-wizard-password-applied").touch()

    boot, command_log = _docker_bootstrap(tmp_path, env)
    _run(["bash", str(boot)], env)

    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert commands == (["init", "run"] if database_name is None else ["run"])
    assert (home / "inhere-box.db").is_file()
    assert not (home / "octop.db").exists()
    if password_hash is not None:
        assert _password_hash(home / "inhere-box.db") == password_hash
    if database_name == "octop.db":
        config = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert config["database"]["sqlite_path"] == "inhere-box.db"
    if move_root:
        assert not home.with_name(".octop").exists()
        assert config["tls"]["cert_file"] == str(home / "tls" / "cert.pem")
        assert (home / "agents/test/.octop/keep.txt").read_text("utf-8") == "harness data"
    files = set(home.rglob("*"))
    command_log.write_text("", encoding="utf-8")
    _run(["bash", str(boot)], env)
    assert command_log.read_text(encoding="utf-8").splitlines() == ["run"]
    assert set(home.rglob("*")) == files


@posix_only
def test_docker_bootstrap_conflicting_databases_prevent_init(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    home = Path(env["INHERE_BOX_HOME"])
    home.mkdir(parents=True)
    for name in ("octop.db", "inhere-box.db"):
        with sqlite3.connect(home / name) as connection:
            connection.execute("CREATE TABLE existing_data (value TEXT)")
            connection.execute("INSERT INTO existing_data VALUES (?)", (name,))
    before = {path: path.read_bytes() for path in home.glob("*.db")}
    boot, command_log = _docker_bootstrap(tmp_path, env)

    result = subprocess.run(
        ["bash", str(boot)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert not command_log.exists()
    assert {path: path.read_bytes() for path in before} == before


@posix_only
@pytest.mark.parametrize("entrypoint", ["native", "docker"])
def test_conflicting_installation_roots_prevent_start(tmp_path: Path, entrypoint: str) -> None:
    if entrypoint == "native":
        launcher, env = _native_launcher(tmp_path)
        command = ["bash", str(launcher), "--prepare"]
        command_log = None
    else:
        env = _environment(tmp_path)
        boot, command_log = _docker_bootstrap(tmp_path, env)
        command = ["bash", str(boot)]
    home = Path(env["INHERE_BOX_HOME"])
    before = {}
    for root in (home, home.with_name(".octop")):
        root.mkdir(parents=True)
        database = root / "inhere-box.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE existing_data (value TEXT)")
            connection.execute("INSERT INTO existing_data VALUES (?)", (root.name,))
        before[database] = database.read_bytes()

    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert "首次启动" not in result.stdout
    assert {path: path.read_bytes() for path in before} == before
    if command_log is not None:
        assert not command_log.exists()


@posix_only
@pytest.mark.parametrize("entrypoint", ["native", "docker"])
def test_pending_password_applies_after_directory_migration(
    tmp_path: Path, entrypoint: str
) -> None:
    launcher, env = _native_launcher(tmp_path)
    _run(["bash", str(launcher), "--prepare"], env)
    home = Path(env["INHERE_BOX_HOME"])
    _legacy_root(home)
    _run(
        [
            "bash",
            "-c",
            'source "$1"; inhere_box_mark_fnos_passwd_pending "$2"',
            "fnos-test",
            str(COMMON_SH),
            str(tmp_path / "data"),
        ],
        env,
    )
    assert not home.exists()
    env["INHERE_BOX_DEFAULT_PASSWORD"] = "ChangedFnosBootstrap789"
    if entrypoint == "native":
        command = ["bash", str(launcher), "--prepare"]
    else:
        boot, _ = _docker_bootstrap(tmp_path, env)
        command = ["bash", str(boot)]

    _run(command, env)

    assert verify_password(
        env["INHERE_BOX_DEFAULT_PASSWORD"], _password_hash(home / "inhere-box.db")
    )
    assert not home.with_name(".octop").exists()
    assert not (home / ".fnos-apply-wizard-password").exists()
    assert not (home.parent / ".inhere-box-apply-wizard-password").exists()


@posix_only
def test_native_cli_wrapper_migrates_before_opening_database(tmp_path: Path) -> None:
    launcher, env = _native_launcher(tmp_path)
    _run(["bash", str(launcher), "--prepare"], env)
    home = Path(env["INHERE_BOX_HOME"])
    _legacy_root(home)
    wrapper = launcher.with_name("inhere-box-cli")
    shutil.copy(REPO / "fnos/native/app/bin/inhere-box-cli", wrapper)

    result = _run(["bash", str(wrapper), "user", "list"], env)

    assert "admin" in result.stdout
    assert not home.with_name(".octop").exists()
    assert (home / "inhere-box.db").is_file()


@posix_only
def test_flat_legacy_copy_runs_directory_path_migration_before_docker_start(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    flat = tmp_path / "flat-legacy"
    _run(
        [
            sys.executable,
            "-m",
            "inhere_box.cli.main",
            "init",
            "--yes",
            "--admin-username",
            "admin",
            "--admin-password",
            env["INHERE_BOX_DEFAULT_PASSWORD"],
        ],
        {**env, "INHERE_BOX_HOME": str(flat)},
    )
    (flat / "inhere-box.db").rename(flat / "octop.db")
    password_hash = _password_hash(flat / "octop.db")
    current = Path(env["INHERE_BOX_HOME"])
    legacy = current.with_name(".octop")
    saved = {
        "database": {"driver": "sqlite", "sqlite_path": str(legacy / "octop.db")},
        "tls": {"cert_file": str(legacy / "cert.pem")},
    }
    (flat / "config.json").write_text(json.dumps(saved), encoding="utf-8")
    (flat / "cert.pem").write_text("existing certificate", encoding="utf-8")
    (flat / ".fnos-wizard-password-applied").touch()
    current.parent.mkdir(parents=True)
    env["FNOS_TEST_FLAT_ROOT"] = str(flat)
    _run(
        [
            "bash",
            "-c",
            'source "$1"; '
            'inhere_box_legacy_docker_data_dir() { printf "%s" "$FNOS_TEST_FLAT_ROOT"; }; '
            'inhere_box_migrate_legacy_docker_data "$HOME"',
            "fnos-test",
            str(COMMON_SH),
        ],
        env,
    )
    assert not current.exists()
    assert json.loads((legacy / "config.json").read_text(encoding="utf-8")) == saved
    boot, log = _docker_bootstrap(tmp_path, env)

    _run(["bash", str(boot)], env)

    migrated = json.loads((current / "config.json").read_text(encoding="utf-8"))
    assert migrated["database"]["sqlite_path"] == str(current / "inhere-box.db")
    assert migrated["tls"]["cert_file"] == str(current / "cert.pem")
    assert (current / "cert.pem").read_text(encoding="utf-8") == "existing certificate"
    assert _password_hash(current / "inhere-box.db") == password_hash
    assert not legacy.exists()
    assert (flat / "octop.db").is_file()
    assert json.loads((flat / "config.json").read_text(encoding="utf-8")) == saved
    assert log.read_text(encoding="utf-8").splitlines() == ["run"]
