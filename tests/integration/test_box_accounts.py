"""Local box claim, independently paired clients, signatures and revocation."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from octop.infra.server import OctopServer
from tests.support.app import octop_client, write_octop_config
from tests.support.box_accounts import (
    _action,
    _admin_login,
    _candidate,
    _claim,
    _login,
    _management_key,
)


@pytest.fixture
async def box_env(
    tmp_octop_home: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, OctopServer, Path]]:
    write_octop_config(tmp_octop_home, account_mode="box_local", enable_api_docs=True)
    async with octop_client(tmp_octop_home) as (client, server):
        yield client, server, tmp_octop_home


@pytest.mark.parametrize("secret_only", [False, True])
async def test_primary_share_guest_and_independent_revocation(box_env, secret_only) -> None:
    client, server, _home = box_env
    first_key, first_id, first_auth = await _claim(client, server)
    account = (await client.get("/api/box/account", headers=first_auth)).json()
    assert account["role"] == "primary"
    assert account["owner"]["username"] == "owner"
    assert server.services.user_repo.get(account["owner"]["id"]).password_hash is None
    proof = await _action(client, first_key, first_id, "enroll", {"request_id": "second-device"})
    before_issue = int(time.time())
    response = await client.post(
        "/api/box/pairing-grants",
        headers=first_auth,
        json={"proof": proof, "request_id": "second-device"},
    )
    assert response.status_code == 200, response.text
    second_key = ec.generate_private_key(ec.SECP256R1())
    grant = response.json()
    assert before_issue + 172800 <= grant["expires_at"] <= int(time.time()) + 172800
    second = await _candidate(
        client,
        {"secret": grant["secret"]} if secret_only else grant,
        second_key,
        request_id="second-candidate",
        name="Laptop",
    )
    assert second["state"] == "consumed" and second["client_id"]
    second_id = second["client_id"]
    second_auth, session = await _login(client, second_key, second_id)
    assert session["role"] == "guest"
    guest_account = (await client.get("/api/box/account", headers=second_auth)).json()
    assert guest_account["owner"] is None
    assert guest_account["user"]["id"] != account["owner"]["id"]
    assert (await client.get("/api/box/clients", headers=second_auth)).status_code == 403
    # Management is Web-only, even the primary's signature cannot revoke a device.
    assert (
        await client.post(f"/api/box/clients/{second_id}/revoke", headers=first_auth, json={})
    ).status_code == 403
    admin = await _admin_login(client, server)
    clients = (await client.get("/api/box/clients", headers=admin)).json()
    assert {item["role"] for item in clients} == {"primary", "guest"}
    response = await client.post(f"/api/box/clients/{first_id}/revoke", headers=admin, json={})
    assert response.status_code == 204, response.text
    assert (await client.get("/api/box/account", headers=first_auth)).status_code == 401
    assert (await client.get("/api/box/account", headers=second_auth)).status_code == 200
    denied = await _action(client, first_key, first_id, "session")
    assert (await client.post("/api/auth/sessions", json={"proof": denied})).status_code == 401
    # The management key remains usable after claiming and revoking the last native client.
    assert (
        await client.post(f"/api/box/clients/{second_id}/revoke", headers=admin, json={})
    ).status_code == 204
    assert (await client.get("/api/box/account", headers=second_auth)).status_code == 401
    assert await _admin_login(client, server)


async def test_box_rejects_old_credentials_and_setup(box_env) -> None:
    client, server, _home = box_env
    _, _, auth = await _claim(client, server)
    for path, body in [
        ("/api/auth/login", {"username": "owner", "password": "anything"}),
        ("/api/auth/change-password", {"old_password": "a", "new_password": "TestPass12"}),
        ("/api/setup/initial-admin", {"username": "other", "password": "TestPass12"}),
        ("/api/auth/invite/redeem", {}),
    ]:
        response = await client.post(path, headers=auth, json=body)
        assert response.status_code == 403, (path, response.text)
    for path in ["/api/auth/oidc/status", "/api/auth/oauth/status", "/api/auth/ldap/status"]:
        response = await client.get(path, headers=auth)
        assert response.status_code == 403, (path, response.text)


async def test_box_docs_have_typed_public_and_protected_operations(box_env) -> None:
    client, server, _home = box_env
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    for path, method in [
        ("/api/box/status", "get"),
        ("/api/box/pairings", "post"),
        ("/api/box/pairings/{pairing_id}/proof", "post"),
        ("/api/auth/challenges", "post"),
        ("/api/auth/sessions", "post"),
    ]:
        operation = paths[path][method]
        assert operation["summary"]
        assert not operation.get("security")
        assert "$ref" in operation["responses"]["200"]["content"]["application/json"]["schema"]
    for path in ["/api/box/account", "/api/box/clients"]:
        assert paths[path]["get"]["security"] == [{"BearerAuth": []}, {"BoxAdminCookie": []}]
    from octop.api.common.box_auth import BOX_CSRF_HEADER, browser_cookie_name

    schemes = response.json()["components"]["securitySchemes"]
    assert schemes["BoxAdminCookie"]["in"] == "cookie"
    assert schemes["BoxAdminCookie"]["name"] == browser_cookie_name(
        server.box_account_service.box_id
    )
    assert schemes["BoxCSRF"]["in"] == "header" and schemes["BoxCSRF"]["name"] == BOX_CSRF_HEADER
    assert paths["/api/auth/browser/session"]["get"]["security"] == [{"BoxAdminCookie": []}]
    for path, method in [
        ("/api/auth/browser/session", "delete"),
        ("/api/auth/browser/refresh", "post"),
        ("/api/box/clients/{client_id}/revoke", "post"),
        ("/api/box/clients/{client_id}/primary", "post"),
        ("/api/box/management-key/rotate", "post"),
    ]:
        operation = paths[path][method]
        assert operation["security"] == [{"BoxAdminCookie": [], "BoxCSRF": []}]
        origin = next(p for p in operation["parameters"] if p["name"] == "Origin")
        assert origin["required"] is True
    assert paths["/api/box/pairing-grants"]["post"]["security"] == [
        {"BearerAuth": []},
        {"BoxAdminCookie": [], "BoxCSRF": []},
    ]
    for path, item in paths.items():
        if path.startswith(("/api/box/", "/api/auth/browser/")):
            for operation in item.values():
                for parameter in operation.get("parameters", []):
                    assert parameter["name"].lower() not in {
                        "access_token",
                        "authorization",
                        "token",
                    }
    # Server-mode generic routes retain their existing dependency contract.
    legacy = paths["/api/auth/me"]["get"]
    assert legacy["security"] == [{"BearerAuth": []}]
    assert any(p["name"] == "access_token" and p["in"] == "query" for p in legacy["parameters"])
    login_headers = paths["/api/auth/browser/sessions"]["post"]["parameters"]
    assert next(p for p in login_headers if p["name"] == "Origin")["required"] is True
    assert next(p for p in login_headers if p["name"] == BOX_CSRF_HEADER)["required"] is False
    assert (await client.get("/api/docs")).status_code == 200


async def test_box_transport_does_not_trust_forwarded_loopback(box_env) -> None:
    client, _server, _home = box_env
    app = client._octop_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.20", 1234)),
        base_url="http://127.0.0.1",
    ) as external:
        response = await external.get(
            "/api/box/status",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "box_tls_required"


async def test_server_mode_cannot_use_box_authentication(app_client) -> None:
    client, _server, _home = app_client
    for path, body in [
        ("/api/auth/challenges", {"client_id": "unknown"}),
        ("/api/auth/sessions", {"proof": "invalid"}),
        ("/api/box/pairings", {}),
    ]:
        response = await client.post(path, json=body)
        assert response.status_code in {403, 404, 503}, response.text


async def test_server_mode_status_discovery_remains_available_over_lan_http(app_client) -> None:
    client, _server, _home = app_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._octop_app, client=("198.51.100.20", 1234)),
        base_url="http://server.test",
    ) as external:
        response = await external.get("/api/box/status")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["reason"] == "box_mode_required"


async def test_native_signature_cannot_be_used_for_web_administration(box_env) -> None:
    client, server, _home = box_env
    key, client_id, auth = await _claim(client, server)
    client.base_url = httpx.URL("http://localhost")
    proof = await _action(client, key, client_id, "session")
    response = await client.post(
        "/api/auth/browser/sessions", headers={"Origin": "http://localhost"}, json={"proof": proof}
    )
    assert response.status_code == 422
    response = await client.post(
        "/api/auth/browser/sessions",
        headers={"Origin": "http://localhost"},
        json={"management_key": proof},
    )
    assert response.status_code in {401, 422}
    assert (await client.get("/api/box/account", headers=auth)).status_code == 200


async def test_approval_endpoints_are_retired(box_env) -> None:
    client, server, _home = box_env
    await _management_key(server)
    admin = await _admin_login(client, server)
    assert (await client.get("/api/box/pairings", headers=admin)).status_code == 410
    for action in ["approve", "cancel"]:
        response = await client.post(f"/api/box/pairings/old/{action}", headers=admin, json={})
        assert response.status_code == 410


async def test_trusted_cli_key_initialization_and_rotation_require_stopped_daemon(tmp_octop_home):
    import asyncio
    import re

    from click.testing import CliRunner

    from octop.cli.main import cli

    write_octop_config(tmp_octop_home, account_mode="box_local")
    runner = CliRunner()
    async with octop_client(tmp_octop_home) as (_client, _server):
        refused = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
        assert refused.exit_code != 0 and not refused.stdout
    initialized = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
    assert initialized.exit_code == 0
    management_key = initialized.stdout.strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", management_key)
    assert initialized.stdout == management_key + "\n" and management_key not in initialized.stderr
    duplicate = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
    assert duplicate.exit_code != 0 and not duplicate.stdout
    async with octop_client(tmp_octop_home) as (client, _server):
        client.base_url = httpx.URL("http://localhost")
        response = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": "http://localhost"},
            json={"management_key": management_key},
        )
        assert response.status_code == 200, response.text
        assert (await client.get("/api/box/status")).json()["primary_client_id"] is None
        refused = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "rotate"])
        assert refused.exit_code != 0 and not refused.stdout
    rotated = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "rotate"])
    assert rotated.exit_code == 0
    new_key = rotated.stdout.strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", new_key) and new_key != management_key
    async with octop_client(tmp_octop_home) as (client, _server):
        client.base_url = httpx.URL("http://localhost")
        for candidate, expected in [(management_key, 401), (new_key, 200)]:
            response = await client.post(
                "/api/auth/browser/sessions",
                headers={"Origin": "http://localhost"},
                json={"management_key": candidate},
            )
            assert response.status_code == expected, response.text
