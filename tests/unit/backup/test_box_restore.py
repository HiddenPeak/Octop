"""Ordinary restore cannot reset box trust or import it as a server account."""

from __future__ import annotations

import shutil
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inhere_box.config import DatabaseConfig
from inhere_box.infra.backup import pg_dump, system_archive
from inhere_box.infra.backup.manifest import BackupManifest
from inhere_box.infra.db import migrate
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.errors import InhereBoxError
from inhere_box.infra.utils.paths import PathLayout


def _pool(root: Path) -> tuple[PathLayout, SqlitePool]:
    paths = PathLayout(root)
    db = SqlitePool(paths.db)
    migrate.run_migrations(db)
    return paths, db


@pytest.mark.parametrize("marker", ["trust.json", "trust-intent.json", "daemon.lock"])
def test_box_target_rejected_before_offline_lock_or_archive_extraction(
    tmp_path, monkeypatch, marker
):
    paths, pool = _pool(tmp_path / "target")
    control = paths.root / "box-auth"
    control.mkdir()
    (control / marker).write_text("preserve")

    def unexpected(*args, **kwargs):
        pytest.fail("box restore proceeded before rejecting local trust state")

    monkeypatch.setattr(system_archive, "offline_restore_guard", unexpected)
    monkeypatch.setattr(system_archive, "_extract_archive", unexpected)
    try:
        with pytest.raises(InhereBoxError) as error:
            system_archive.restore_system_backup(
                b"not an archive", paths=paths, pool=pool, db_config=DatabaseConfig()
            )
        assert error.value.status == 409
        assert error.value.details["reason"] == "box_restore_requires_recovery"
        assert (control / marker).read_text() == "preserve"
    finally:
        pool.close()


def test_box_database_without_trust_files_is_still_not_a_server_target(tmp_path, monkeypatch):
    paths, pool = _pool(tmp_path / "target")
    try:
        with pool.transaction() as conn:
            conn.execute("INSERT INTO box_account(singleton,box_id) VALUES(1,'old-box')")

        def unexpected(*args, **kwargs):
            pytest.fail("deleted box trust files must not enable ordinary restore")

        monkeypatch.setattr(system_archive, "offline_restore_guard", unexpected)
        with pytest.raises(InhereBoxError) as error:
            system_archive.restore_system_backup(
                b"invalid", paths=paths, pool=pool, db_config=DatabaseConfig()
            )
        assert error.value.details["reason"] == "box_restore_requires_recovery"
    finally:
        pool.close()


@pytest.mark.parametrize("restore_config", [False, True])
@pytest.mark.parametrize("preserve_users", [False, True])
def test_box_snapshot_cannot_be_imported_into_fresh_server(
    tmp_path, monkeypatch, restore_config, preserve_users
):
    source, source_pool = _pool(tmp_path / "source")
    target, target_pool = _pool(tmp_path / "target")
    archive = tmp_path / "box.tar.gz"
    try:
        with source_pool.transaction() as conn:
            conn.execute("INSERT INTO box_account(singleton,box_id) VALUES(1,'source-box')")
        # No config/manifest account-mode marker is needed to detect box state.
        system_archive.create_system_backup(
            paths=source,
            agent_rows=[],
            pool=source_pool,
            db_config=DatabaseConfig(),
            dest=archive,
            include_config=False,
        )
        target.config.write_text('{"name":"unchanged"}')
        with target_pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('keep','hash','admin',1)"
            )

        def unexpected(*args, **kwargs):
            pytest.fail("box snapshot reached a live database write")

        monkeypatch.setattr(system_archive, "restore_sqlite_into_pool", unexpected)
        monkeypatch.setattr(system_archive, "capture_chat_tables", unexpected)
        with pytest.raises(InhereBoxError) as error:
            system_archive.restore_system_backup(
                archive,
                paths=target,
                pool=target_pool,
                db_config=DatabaseConfig(),
                restore_config=restore_config,
                preserve_users=preserve_users,
            )
        assert error.value.details["reason"] == "box_restore_requires_recovery"
        assert target.config.read_text() == '{"name":"unchanged"}'
        with target_pool.connect() as conn:
            assert conn.execute("SELECT username FROM users").fetchone()[0] == "keep"
            assert conn.execute("SELECT COUNT(*) FROM box_account").fetchone()[0] == 0
    finally:
        source_pool.close()
        target_pool.close()


@pytest.mark.parametrize("version", [60, 61, 62, 63])
def test_normal_server_backups_with_missing_or_empty_box_tables_remain_restorable(
    tmp_path, monkeypatch, version
):
    source = PathLayout(tmp_path / "source")
    source_pool = SqlitePool(source.db)
    target, target_pool = _pool(tmp_path / "target")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for number, path in migrate._discover("sqlite"):
        if number <= version:
            shutil.copy2(path, migrations / path.name)
    archive = tmp_path / "server.tar.gz"
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", migrations)
            migrate.run_migrations(source_pool)
        with source_pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('server-user','hash','admin',1)"
            )
        system_archive.create_system_backup(
            paths=source,
            agent_rows=[],
            pool=source_pool,
            db_config=DatabaseConfig(),
            dest=archive,
        )
        system_archive.restore_system_backup(
            archive,
            paths=target,
            pool=target_pool,
            db_config=DatabaseConfig(),
        )
        with target_pool.connect() as conn:
            assert conn.execute("SELECT username FROM users").fetchone()[0] == "server-user"
    finally:
        source_pool.close()
        target_pool.close()


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("SET client_encoding = 'UTF8';\n", False),
        ("COPY public.box_account (singleton,box_id) FROM stdin;\n\\.\n", False),
        ("COPY public.box_account (singleton,box_id) FROM stdin;\n1\tbox-id\n\\.\n", True),
        ("INSERT INTO public.box_account VALUES (1, 'box-id');\n", True),
    ],
)
def test_pg_preflight_renders_selected_data_without_connecting_to_database(
    tmp_path, monkeypatch, script, expected
):
    monkeypatch.setattr(pg_dump, "_require_tool", lambda _: "pg_restore")

    def run(command, **kwargs):
        assert "--dbname" not in command and "-d" not in command
        assert "--clean" not in command and "--data-only" in command
        assert "box_account" in command
        Path(command[command.index("--file") + 1]).write_text(script)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pg_dump.subprocess, "run", run)
    assert pg_dump.postgres_snapshot_has_box_state(tmp_path / "archive.dump") is expected


def test_pg_preflight_failure_refuses_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(pg_dump, "_require_tool", lambda _: "pg_restore")
    monkeypatch.setattr(
        pg_dump.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    with pytest.raises(InhereBoxError) as error:
        pg_dump.postgres_snapshot_has_box_state(tmp_path / "archive.dump")
    assert error.value.details["reason"] == "box_restore_preflight_failed"


def test_box_postgres_archive_rejected_before_restore_process(tmp_path, monkeypatch):
    paths = PathLayout(tmp_path / "target")
    manifest = BackupManifest(
        manifest_version=1,
        inhere_box_version="1.0",
        schema_version=61,
        created_at="now",
        home="",
        db_file="db/inhere_box.dump",
        database_driver="postgresql",
        database_dump_format="pg_custom",
    )
    archive = tmp_path / "box-pg.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, data in [
            ("manifest.json", manifest.to_json().encode()),
            ("db/inhere_box.dump", b"dump"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            output.addfile(member, BytesIO(data))
    monkeypatch.setattr(system_archive, "pool_has_box_state", lambda _: False)
    monkeypatch.setattr(system_archive, "postgres_snapshot_has_box_state", lambda _: True)

    def unexpected(*args, **kwargs):
        pytest.fail("box archive reached pg_restore against a real database")

    monkeypatch.setattr(system_archive, "restore_postgres", unexpected)
    with pytest.raises(InhereBoxError) as error:
        system_archive.restore_system_backup(
            archive,
            paths=paths,
            pool=SimpleNamespace(dialect="postgresql"),
            db_config=DatabaseConfig(),
        )
    assert error.value.details["reason"] == "box_restore_requires_recovery"


def test_cli_rejects_box_restore_before_confirmation_or_opening_database(tmp_path, monkeypatch):
    from inhere_box.cli.commands import backup

    paths = PathLayout(tmp_path / "box")
    (paths.root / "box-auth").mkdir(parents=True)
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"not used")

    def unexpected(*args, **kwargs):
        pytest.fail("box restore opened a database")

    monkeypatch.setattr(backup, "open_database", unexpected)
    result = CliRunner().invoke(backup.backup, ["restore", str(archive), "--home", str(paths.root)])
    assert result.exit_code != 0
    assert "Continue?" not in result.output
