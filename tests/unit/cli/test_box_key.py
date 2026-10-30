"""Trusted terminal key maintenance is serialized with the real daemon lock."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from octop.cli.commands.box import box
from octop.config import OctopConfig
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.auth.box_accounts import BoxAccountService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def initialized_box(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    paths = PathLayout(tmp_path)
    paths.config.write_text(json.dumps({"account_mode": "box_local", "bind_host": "127.0.0.1"}))
    identity = get_or_create_identity(paths.agentcomputer_identity)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(
        db=pool, paths=paths, config=OctopConfig(account_mode="box_local")
    )
    secret = services.secret_repo.get_or_create("jwt", lambda: b"test" * 8)
    service = BoxAccountService(services, secret=secret, box_id=str(identity.boxID))
    service.initialize()
    yield paths, service
    pool.close()


def test_cli_key_init_once_and_rotation_print_only_new_secret(initialized_box):
    _, service = initialized_box
    runner = CliRunner()
    first = runner.invoke(box, ["key", "init", "--locale", "en"])
    assert first.exit_code == 0, first.output
    secret = first.stdout.strip()
    assert len(secret) == 43 and secret not in first.stderr
    assert "shown once" in first.stderr
    session = service.create_admin_session(secret)
    assert runner.invoke(box, ["key", "init"]).exit_code != 0
    rotated = runner.invoke(box, ["key", "rotate", "--revoke-clients", "--locale", "en"])
    assert rotated.exit_code == 0, rotated.output
    assert len(rotated.stdout.strip()) == 43 and rotated.stdout.strip() != secret
    assert secret not in rotated.output
    with pytest.raises(OctopError):
        service.validate_access_token(session["access_token"])
    with pytest.raises(OctopError):
        service.create_admin_session(secret)
    assert service.create_admin_session(rotated.stdout.strip())["session_kind"] == "admin"


def test_cli_refuses_held_daemon_lock_before_migrations(initialized_box, monkeypatch):
    paths, service = initialized_box
    lock = ResidentWorkerLock(paths.root / "box-auth" / "daemon.lock")
    lock.acquire()

    def unexpected(_):
        pytest.fail("migration must not run against the active daemon")

    monkeypatch.setattr("octop.cli.commands.box.run_migrations", unexpected)
    try:
        result = CliRunner().invoke(box, ["key", "init", "--locale", "en"])
        assert result.exit_code != 0 and "Stop this Octop instance" in result.output
        assert service.status()["state"] == "unclaimed"
    finally:
        lock.release()


def test_retired_pair_cannot_issue_any_credential(initialized_box):
    _, service = initialized_box
    result = CliRunner().invoke(
        box, ["pair", "--url", "http://localhost", "--secret-only", "--locale", "en"]
    )
    assert result.exit_code != 0 and "octop box key init" in result.output
    assert service.status()["state"] == "unclaimed"
