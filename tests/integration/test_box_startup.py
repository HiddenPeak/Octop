"""Lost trust and mode changes must stop the box before background execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from inhere_box.infra.agentcomputer.identity import IdentityError
from inhere_box.infra.errors import InhereBoxError
from inhere_box.infra.server import InhereBoxServer
from tests.support.app import inhere_box_client, write_inhere_box_config
from tests.support.auth import bootstrap_admin, login
from tests.support.box_accounts import _claim


@pytest.mark.parametrize("damage", ["identity", "trust", "mode"])
async def test_existing_box_fails_closed_before_workers_and_releases_lock(
    tmp_inhere_box_home: Path, damage: str
) -> None:
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        await _claim(client, server)
        identity_path = server.paths.agentcomputer_identity
    trust_path = tmp_inhere_box_home / "box-auth" / "trust.json"
    if damage == "identity":
        identity_path.unlink()
        expected = IdentityError
    elif damage == "trust":
        trust_path.unlink()
        expected = InhereBoxError
    else:
        write_inhere_box_config(tmp_inhere_box_home, account_mode="server")
        expected = ValueError

    for _attempt in range(2):
        restarted = InhereBoxServer(home=tmp_inhere_box_home)
        with pytest.raises(expected):
            await restarted.start()
        assert restarted.project_task_worker is None
        assert restarted._box_auth_lock is None
        if damage == "identity":
            assert not identity_path.exists()
        if damage == "trust":
            assert not trust_path.exists()


async def test_unsupported_server_conversion_does_not_mutate_box_identity(
    tmp_inhere_box_home: Path,
) -> None:
    async with inhere_box_client(tmp_inhere_box_home) as (client, _server):
        await bootstrap_admin(client, tmp_inhere_box_home)
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    restarted = InhereBoxServer(home=tmp_inhere_box_home)
    with pytest.raises(InhereBoxError) as error:
        await restarted.start()
    assert error.value.details["reason"] == "box_migration_required"
    assert not (tmp_inhere_box_home / "box-auth").exists()
    assert restarted.project_task_worker is None
    write_inhere_box_config(tmp_inhere_box_home, account_mode="server")
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        assert await login(client)
        with server.services.db.connect() as conn:
            assert server.services.box_account_repo.account(conn) is None


async def test_legacy_account_requires_local_upgrade_and_preserves_owner_projects(
    tmp_inhere_box_home,
):
    import asyncio
    import time

    import httpx
    import jwt
    from click.testing import CliRunner

    from inhere_box.cli.main import cli
    from inhere_box.infra.auth.box_accounts.service import AUDIENCE
    from inhere_box.infra.projects.models import ProjectFields
    from inhere_box.infra.projects.service import create_project
    from tests.support.box_accounts import _legacy_account

    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with inhere_box_client(tmp_inhere_box_home) as (_client, server):
        box = server.box_account_service
        owner_id = await asyncio.to_thread(_legacy_account, box)
        project = await asyncio.to_thread(
            create_project,
            server.services,
            owner_id,
            ProjectFields(name="Preserved legacy project"),
            "legacy-project-seed",
        )
        now = int(time.time())
        old_token = jwt.encode(
            {
                "sub": str(owner_id),
                "uname": "owner",
                "role": "admin",
                "iss": box.box_id,
                "aud": AUDIENCE,
                "sid": "old-session",
                "paired_client_id": "legacy",
                "auth_revision": 1,
                "boot_id": "old-boot",
                "iat": now,
                "exp": now + 600,
            },
            server.services.secret_repo.get("jwt"),
            algorithm="HS256",
        )
    restarted = InhereBoxServer(home=tmp_inhere_box_home)
    with pytest.raises(InhereBoxError):
        await restarted.start()
    assert restarted.project_task_worker is None and restarted._box_auth_lock is None
    runner = CliRunner()
    refused = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
    assert refused.exit_code != 0 and not refused.stdout
    upgraded = await asyncio.to_thread(
        runner.invoke, cli, ["box", "key", "init", "--upgrade-legacy"]
    )
    assert upgraded.exit_code == 0
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        client.base_url = httpx.URL("http://localhost")
        assert (
            await client.get("/api/box/account", headers={"Authorization": "Bearer " + old_token})
        ).status_code == 401
        logged_in = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": "http://localhost"},
            json={"management_key": upgraded.stdout.strip()},
        )
        assert logged_in.status_code == 200, logged_in.text
        headers = {
            "Origin": "http://localhost",
            "X-Inhere-Box-CSRF": logged_in.json()["csrf_token"],
        }
        account = (await client.get("/api/box/account", headers=headers)).json()
        assert account["owner"]["id"] == owner_id and account["primary_client_id"] is None
        projects = (await client.get("/api/projects", headers=headers)).json()
        assert projects[0]["project_id"] == project.project_id
        clients = (await client.get("/api/box/clients", headers=headers)).json()
        assert clients[0]["role"] == "legacy" and clients[0]["state"] == "revoked"
        with server.services.db.connect() as conn:
            old_session = server.services.box_account_repo.session(conn, "old-session")
            assert old_session["revoked_at"] is not None
