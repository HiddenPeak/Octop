"""Only audited live pointers change inside an unpublished installation copy."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from inhere_box.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.repos.secrets import SecretRepo
from inhere_box.infra.setup.branding_paths import (
    BrandingPathsError,
    rewrite_installation_paths,
)
from inhere_box.infra.utils.env_file import parse_env_text


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, staging, target = (tmp_path / name for name in (".octop", "staging", ".inhere-box"))
    source.mkdir()
    staging.mkdir()
    (source / "untouched").write_bytes(b"original source")
    return source, staging, target


def _database(staging: Path, name: str = "octop.db") -> Path:
    path = staging / name
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            "CREATE TABLE agents(config_json TEXT);"
            "CREATE TABLE projects(directory TEXT, client_directory TEXT);"
            "CREATE TABLE project_task_turns(configuration_json TEXT,prompt TEXT);"
            "CREATE TABLE storage_backends(kind TEXT,bucket TEXT,config_json TEXT);"
            "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE threads(artifacts TEXT);"
            "CREATE TABLE connectors(kind TEXT,credential_blob BLOB);"
            "CREATE TABLE secrets(k TEXT PRIMARY KEY,v BLOB,created_at INTEGER);"
        )
    return path


@pytest.mark.parametrize("database_name", ["octop.db", "inhere-box.db"])
def test_audited_database_paths_change_but_client_and_history_text_do_not(
    tmp_path: Path, database_name: str
) -> None:
    source, stage, target = _roots(tmp_path)
    db = _database(stage, database_name)
    old = str(source / "agents" / "a")
    new = str(target / "agents" / "a")
    agent = {
        "workspace_dir": old,
        "backend": {
            "type": "composite",
            "default": {"type": "local_shell", "root_dir": old},
            "routes": {
                "/local/": {"type": "filesystem", "root_dir": old},
                "/remote/": {"type": "s3", "prefix": old},
            },
        },
        "memory": {"backend": {"type": "sqlite", "db_path": old + "/memory.sqlite"}},
        "skills": [old + "/skills", "/.octop/skills", "relative/skills"],
        "system_prompt": "Do not rewrite " + old,
        "system_files_path": ".octop",
    }
    turn = {
        "directory": old,
        "client_directory": old,
        "project_defaults": {"directory": old, "client_directory": old},
        "expert_profile": {"system_prompt": old},
        "arguments": {"file_path": old},
    }
    runners = {
        "runners": {
            "cursor_cli": {
                "command": old + "/runner",
                "args": [old + "/script", "literal=" + old],
                "env": {"PATH": os.pathsep.join([old, "/usr/bin"]), "TOKEN": old},
            }
        }
    }
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO agents VALUES(?)", (json.dumps(agent),))
        conn.execute("INSERT INTO projects VALUES(?,?)", (old, old))
        conn.execute("INSERT INTO project_task_turns VALUES(?,?)", (json.dumps(turn), old))
        for kind in ("filesystem", "shell", "s3"):
            conn.execute(
                "INSERT INTO storage_backends VALUES(?,?,?)",
                (kind, old, json.dumps({"root_dir": old, "path": old, "prefix": old})),
            )
        conn.execute(
            "INSERT INTO settings VALUES(?,?)", ("acp_runners:user:1", json.dumps(runners))
        )
        conn.execute(
            "INSERT INTO settings VALUES(?,?)",
            (
                "acp_setup:user:1:cursor_cli",
                json.dumps({"managed_launch": [old + "/runner", "acp"]}),
            ),
        )
        conn.execute("INSERT INTO settings VALUES('unrelated',?)", (json.dumps({"path": old}),))
        conn.execute(
            "INSERT INTO threads VALUES(?)",
            (
                json.dumps(
                    [old + "/out.txt", {"path": old + "/two.txt", "agent_id": "a"}, "relative.txt"]
                ),
            ),
        )

    counts = rewrite_installation_paths(stage, source, target)

    with closing(sqlite3.connect(db)) as conn:
        actual = json.loads(conn.execute("SELECT config_json FROM agents").fetchone()[0])
        assert actual["workspace_dir"] == new
        assert actual["backend"]["default"]["root_dir"] == new
        assert actual["backend"]["routes"]["/local/"]["root_dir"] == new
        assert actual["backend"]["routes"]["/remote/"]["prefix"] == old
        assert actual["memory"]["backend"]["db_path"] == str(Path(new) / "memory.sqlite")
        assert actual["skills"] == [str(Path(new) / "skills"), "/.octop/skills", "relative/skills"]
        assert actual["system_prompt"] == agent["system_prompt"]
        assert actual["system_files_path"] == ".octop"
        assert conn.execute("SELECT * FROM projects").fetchone() == (new, old)
        stored, prompt = conn.execute("SELECT * FROM project_task_turns").fetchone()
        actual_turn = json.loads(stored)
        assert actual_turn["directory"] == actual_turn["project_defaults"]["directory"] == new
        assert (
            actual_turn["client_directory"]
            == actual_turn["project_defaults"]["client_directory"]
            == old
        )
        assert actual_turn["arguments"] == turn["arguments"] and prompt == old
        storage = conn.execute("SELECT kind,bucket,config_json FROM storage_backends").fetchall()
        assert [row[1] for row in storage] == [new, new, old]
        assert all(json.loads(row[2])["prefix"] == old for row in storage)
        settings = dict(conn.execute("SELECT key,value FROM settings"))
        launch = json.loads(settings["acp_runners:user:1"])["runners"]["cursor_cli"]
        assert launch["command"] == str(Path(new) / "runner")
        assert launch["args"] == [str(Path(new) / "script"), "literal=" + old]
        assert launch["env"]["PATH"] == os.pathsep.join([new, "/usr/bin"])
        assert launch["env"]["TOKEN"] == old
        assert json.loads(settings["unrelated"])["path"] == old
        assert json.loads(settings["acp_setup:user:1:cursor_cli"])["managed_launch"][0] == str(
            Path(new) / "runner"
        )
        assert json.loads(conn.execute("SELECT artifacts FROM threads").fetchone()[0]) == [
            str(Path(new) / "out.txt"),
            {"path": str(Path(new) / "two.txt"), "agent_id": "a"},
            "relative.txt",
        ]
    assert counts["agents"] == counts["projects"] == counts["project_task_turns"] == 1
    assert not target.exists() and (source / "untouched").read_bytes() == b"original source"
    repeated = rewrite_installation_paths(stage, source, target)
    assert repeated == {"sqlite_checkpoints": 1}


@pytest.mark.parametrize(
    "database_path", ["octop.db", "./octop.db", "absolute_old", "absolute_new"]
)
def test_config_env_history_identity_and_sqlite_wal_are_relocated(
    tmp_path: Path, database_path: str
) -> None:
    source, stage, target = _roots(tmp_path)
    _database(stage)
    configured = (
        str(source / ("octop.db" if database_path == "absolute_old" else "inhere-box.db"))
        if database_path.startswith("absolute")
        else database_path
    )
    (stage / "config.json").write_text(
        json.dumps(
            {
                "database": {"sqlite_path": configured},
                "tls": {"cert_file": str(source / "ssl" / "cert.pem"), "key_file": "ssl/key.pem"},
                "description": str(source),
            }
        )
    )
    (stage / "env").write_text(
        f'INHERE_BOX_HOME="{source}"\nOCTOP_DATABASE_SQLITE_PATH="{source / "octop.db"}"\nTOKEN="{source}"\n'
    )
    history = stage / "history_v2.sqlite"
    with closing(sqlite3.connect(history)) as conn, conn:
        conn.execute("CREATE TABLE archive_meta(version INTEGER,identity TEXT)")
        conn.execute("INSERT INTO archive_meta VALUES(1,?)", (str(source / "octop.db"),))
        conn.execute("CREATE TABLE bodies(value TEXT)")
        conn.execute("INSERT INTO bodies VALUES(?)", (str(source),))
    memory = stage / "agents" / "a" / "memory.sqlite"
    memory.parent.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE facts(body TEXT)');c.execute(\"INSERT INTO facts VALUES('committed in WAL')\");c.commit();os._exit(0)",
            str(memory),
        ],
        check=True,
    )
    assert Path(str(memory) + "-wal").exists()

    rewrite_installation_paths(stage, source, target)

    config = json.loads((stage / "config.json").read_text())
    assert config["database"]["sqlite_path"] == (
        str(target / "inhere-box.db") if database_path.startswith("absolute") else "inhere-box.db"
    )
    assert config["tls"] == {
        "cert_file": str(target / "ssl" / "cert.pem"),
        "key_file": "ssl/key.pem",
    }
    assert config["description"] == str(source)
    env = parse_env_text((stage / "env").read_text())
    assert env["INHERE_BOX_HOME"] == str(target)
    assert env["OCTOP_DATABASE_SQLITE_PATH"] == str(target / "inhere-box.db")
    assert env["TOKEN"] == str(source)
    with closing(sqlite3.connect(history)) as conn:
        assert conn.execute("SELECT identity FROM archive_meta").fetchone()[0] == str(
            target / "inhere-box.db"
        )
        assert conn.execute("SELECT value FROM bodies").fetchone()[0] == str(source)
    with closing(sqlite3.connect(memory)) as conn:
        assert conn.execute("SELECT body FROM facts").fetchone()[0] == "committed in WAL"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not Path(str(memory) + "-wal").exists()


def test_custom_mcp_credentials_are_decrypted_and_reencrypted_without_touching_secrets(
    tmp_path: Path,
) -> None:
    source, stage, target = _roots(tmp_path)
    db = _database(stage)
    pool = SqlitePool(db)
    try:
        repo = SecretRepo(pool)
        payload = {
            "servers": {
                "local": {
                    "transport": "stdio",
                    "command": str(source / "mcp"),
                    "args": [str(source / "server.py")],
                    "env": {"TOKEN": "private-token"},
                },
                "remote": {
                    "transport": "streamable_http",
                    "url": "https://example.test",
                    "headers": {"Authorization": "private-token"},
                },
            }
        }
        blob = encrypt_credentials(repo, payload)
        key = repo.get("connector_fernet")
        with pool.transaction() as conn:
            conn.execute("INSERT INTO connectors VALUES('custom-mcp',?)", (blob,))
    finally:
        pool.close()

    assert rewrite_installation_paths(stage, source, target)["custom_mcp"] == 1

    pool = SqlitePool(db)
    try:
        repo = SecretRepo(pool)
        with pool.connect() as conn:
            updated = bytes(conn.execute("SELECT credential_blob FROM connectors").fetchone()[0])
        assert updated != blob and b"private-token" not in updated
        result = decrypt_credentials(repo, updated)
        assert result["servers"]["local"]["command"] == str(target / "mcp")
        assert result["servers"]["local"]["args"] == [str(target / "server.py")]
        assert result["servers"]["local"]["env"] == {"TOKEN": "private-token"}
        assert result["servers"]["remote"] == payload["servers"]["remote"]
        assert repo.get("connector_fernet") == key
    finally:
        pool.close()


def test_unreadable_custom_mcp_never_silently_skips_path_migration(tmp_path: Path) -> None:
    source, stage, target = _roots(tmp_path)
    db = _database(stage)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO connectors VALUES('custom-mcp',?)", (b"invalid-token",))
    with pytest.raises(BrandingPathsError, match="Cannot safely inspect custom MCP"):
        rewrite_installation_paths(stage, source, target)


def test_rejects_unrelated_archive_or_both_control_databases(tmp_path: Path) -> None:
    source, stage, target = _roots(tmp_path)
    old = _database(stage)
    (stage / "inhere-box.db").write_bytes(old.read_bytes())
    with pytest.raises(BrandingPathsError, match="Both control-plane"):
        rewrite_installation_paths(stage, source, target)
    (stage / "inhere-box.db").unlink()
    with closing(sqlite3.connect(stage / "history_v2.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE archive_meta(identity TEXT)")
        conn.execute("INSERT INTO archive_meta VALUES('unrelated database')")
    with pytest.raises(BrandingPathsError, match="different control-plane identity"):
        rewrite_installation_paths(stage, source, target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX generated venv launchers and symlinks")
def test_venv_generated_launchers_and_absolute_symlinks_move_but_user_text_does_not(
    tmp_path: Path,
) -> None:
    source, stage, target = _roots(tmp_path)
    bindir = stage / "venv" / "bin"
    bindir.mkdir(parents=True)
    launcher = bindir / "inhere-box"
    launcher.write_text(f'#!{source}/venv/bin/python\nprint("{source}")\n')
    launcher.chmod(0o755)
    (bindir / "activate").write_text(f'VIRTUAL_ENV="{source}/venv"\nexport VIRTUAL_ENV\n')
    (stage / "venv" / "pyvenv.cfg").write_text(
        f"home = /usr/bin\ncommand = /usr/bin/python -m venv {source}/venv\n"
    )
    (stage / "skill-link").symlink_to(source / "skill-packages" / "a")
    rewrite_installation_paths(stage, source, target)
    assert launcher.read_text() == f'#!{target}/venv/bin/python\nprint("{source}")\n'
    assert launcher.stat().st_mode & 0o111
    assert (bindir / "activate").read_text().startswith(f'VIRTUAL_ENV="{target}/venv"')
    assert str(target / "venv") in (stage / "venv" / "pyvenv.cfg").read_text()
    assert os.readlink(stage / "skill-link") == str(target / "skill-packages" / "a")


def test_windows_embedded_venv_launcher_paths_require_rebuilding(tmp_path: Path) -> None:
    source, stage, target = _roots(tmp_path)
    scripts = stage / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "inhere-box.exe").write_bytes(
        b"MZ\x00" + str(source / "venv" / "python.exe").encode()
    )
    with pytest.raises(BrandingPathsError, match="rebuild the venv"):
        rewrite_installation_paths(stage, source, target)


def test_unmodified_user_sqlite_modes_and_backups_remain_unchanged(tmp_path: Path) -> None:
    source, stage, target = _roots(tmp_path)
    _database(stage)
    user_db = stage / "user.sqlite"
    with closing(sqlite3.connect(user_db)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE data(value TEXT)")
    before = user_db.read_bytes()
    backup = stage / "backups"
    backup.mkdir()
    snapshot = backup / "untouched.sqlite"
    snapshot.write_bytes(user_db.read_bytes())
    rewrite_installation_paths(stage, source, target)
    assert user_db.read_bytes() == before
    assert snapshot.read_bytes() == before
    with closing(sqlite3.connect(user_db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("location", ["config", "process", "env", ".env"])
@pytest.mark.parametrize("setting", ["driver", "url"])
def test_external_control_plane_requires_separate_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, setting: str
) -> None:
    source, stage, target = _roots(tmp_path)
    config = stage / "config.json"
    config.write_text(json.dumps({"database": {"sqlite_path": "octop.db"}}))
    if location == "config":
        # File URLs do not select the driver; the file's driver is authoritative.
        config.write_text(json.dumps({"database": {"driver": "postgresql"}}))
    else:
        key = "INHERE_BOX_DATABASE_" + setting.upper()
        value = "postgresql" if setting == "driver" else "postgresql://db.example/inhere"
        if location == "process":
            monkeypatch.setenv(key, value)
        else:
            (stage / location).write_text(f"{key}={value}\n")
    before = config.read_bytes()
    with pytest.raises(BrandingPathsError, match="External database"):
        rewrite_installation_paths(stage, source, target)
    assert config.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_staging_config_symlink_cannot_mutate_the_source(tmp_path: Path) -> None:
    source, stage, target = _roots(tmp_path)
    config = source / "config.json"
    config.write_text(json.dumps({"database": {"sqlite_path": "octop.db"}}))
    original = config.read_bytes()
    (stage / "config.json").symlink_to(config)
    with pytest.raises(BrandingPathsError, match="regular staging file"):
        rewrite_installation_paths(stage, source, target)
    assert config.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="SQLite sidecar symlink semantics")
@pytest.mark.parametrize("database", ["octop.db", "history_v2.sqlite", "memory.sqlite"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_sidecar_symlinks_are_refused_before_opening(
    tmp_path: Path, database: str, suffix: str
) -> None:
    source, stage, target = _roots(tmp_path)
    external = tmp_path / "external"
    external.write_bytes(b"external bytes must not change")
    if database == "octop.db":
        path = _database(stage)
    else:
        path = stage / database
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE archive_meta(identity TEXT)")
    sidecar = Path(str(path) + suffix)
    sidecar.symlink_to(external)
    original = path.read_bytes()

    with pytest.raises(BrandingPathsError, match="regular staging file"):
        rewrite_installation_paths(stage, source, target)

    assert external.read_bytes() == b"external bytes must not change"
    assert path.read_bytes() == original
    assert sidecar.is_symlink()
