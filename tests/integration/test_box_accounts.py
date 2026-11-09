"""Local box claim, independently paired clients, signatures and revocation."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from inhere_box.infra.server import InhereBoxServer
from tests.support.app import inhere_box_client, write_inhere_box_config
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
    tmp_inhere_box_home: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, InhereBoxServer, Path]]:
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local", enable_api_docs=True)
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        yield client, server, tmp_inhere_box_home


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
    # An owner Bearer alone cannot revoke a device.
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


@pytest.mark.parametrize("target_role", ["primary", "guest"])
async def test_owner_revocation_binds_the_caller_and_target_and_preserves_data(
    box_env, target_role
) -> None:
    from tests.integration.test_project_task_imports import imported, project, source_session

    client, server, _home = box_env
    owner_key, owner_id, owner_auth = await _claim(client, server)
    other_key, other_id, other_auth = await _claim(client, server, request_id="owner-two")
    share_proof = await _action(
        client, owner_key, owner_id, "enroll", {"request_id": "guest-share"}
    )
    grant = await client.post(
        "/api/box/pairing-grants",
        headers=owner_auth,
        json={"request_id": "guest-share", "proof": share_proof},
    )
    assert grant.status_code == 200, grant.text
    guest_key = ec.generate_private_key(ec.SECP256R1())
    guest = await _candidate(client, grant.json(), guest_key, request_id="revoke-guest")
    guest_id = guest["client_id"]
    guest_auth, _ = await _login(client, guest_key, guest_id)

    saved_project = await project(client, owner_auth)
    project_id = saved_project["project_id"]
    task = await imported(client, owner_auth, source_session(project_id=project_id))
    task_path = f"/api/project-tasks/{task['task_id']}"
    before = await client.get(task_path, headers=owner_auth)
    assert before.status_code == 200, before.text
    assert before.json()["turns"][0]["reply"] == "Imported answer 1"

    target_id, target_auth = (
        (other_id, other_auth) if target_role == "primary" else (guest_id, guest_auth)
    )
    unrelated_id, unrelated_auth = (
        (guest_id, guest_auth) if target_role == "primary" else (other_id, other_auth)
    )
    target_path = f"/api/box/clients/{target_id}/revoke"
    params = {"target_client_id": target_id}
    proof = await _action(client, owner_key, owner_id, "revoke", params)
    wrong_signature = await _action(client, other_key, owner_id, "revoke", params)
    wrong_device = await _action(client, other_key, other_id, "revoke", params)
    wrong_action = await _action(client, owner_key, owner_id, "session")
    self_proof = await _action(
        client, owner_key, owner_id, "revoke", {"target_client_id": owner_id}
    )
    guest_proof = await _action(
        client, guest_key, guest_id, "revoke", {"target_client_id": owner_id}
    )

    assert (await client.post(target_path, headers=owner_auth, json={})).status_code == 403
    for path, auth, signed, status in [
        (target_path, owner_auth, wrong_signature, 401),
        (target_path, owner_auth, wrong_device, 401),
        (target_path, owner_auth, wrong_action, 401),
        (f"/api/box/clients/{unrelated_id}/revoke", owner_auth, proof, 401),
        (f"/api/box/clients/{owner_id}/revoke", owner_auth, self_proof, 403),
        (f"/api/box/clients/{owner_id}/revoke", guest_auth, guest_proof, 403),
    ]:
        denied = await client.post(path, headers=auth, json={"proof": signed})
        assert denied.status_code == status, denied.text
    assert (await client.get("/api/box/account", headers=target_auth)).status_code == 200

    revoked = await client.post(target_path, headers=owner_auth, json={"proof": proof})
    assert revoked.status_code == 204, revoked.text
    assert (
        await client.post(target_path, headers=owner_auth, json={"proof": proof})
    ).status_code == 401
    fresh_proof = await _action(client, owner_key, owner_id, "revoke", params)
    assert (
        await client.post(target_path, headers=owner_auth, json={"proof": fresh_proof})
    ).status_code == 204
    assert (await client.get("/api/box/account", headers=target_auth)).status_code == 401
    assert (await client.get("/api/box/account", headers=unrelated_auth)).status_code == 200
    device_list = await client.get("/api/box/clients", headers=owner_auth)
    assert device_list.status_code == 200
    assert {item["client_id"]: item["state"] for item in device_list.json()} == {
        owner_id: "active",
        target_id: "revoked",
        unrelated_id: "active",
    }
    assert (
        await client.get(f"/api/projects/{project_id}", headers=owner_auth)
    ).json() == saved_project
    assert (await client.get(task_path, headers=owner_auth)).json()["turns"] == before.json()[
        "turns"
    ]
    audit = server.services.audit_repo.query(action="box.client.revoke")
    assert [(row.actor, row.target) for row in audit] == [(owner_id, target_id)]


async def test_expired_owner_revocation_proof_allows_retry_with_same_session(
    box_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inhere_box.infra.auth.box_accounts.service import CHALLENGE_TTL

    client, server, _home = box_env
    owner_key, owner_id, owner_auth = await _claim(client, server)
    other_key, other_id, other_auth = await _claim(client, server, request_id="owner-two")
    path = f"/api/box/clients/{other_id}/revoke"
    params = {"target_client_id": other_id}
    stale = await _action(client, owner_key, owner_id, "revoke", params)
    forged = await _action(client, other_key, owner_id, "revoke", params)
    after_expiration = time.time() + CHALLENGE_TTL
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: after_expiration)
        invalid = await client.post(path, headers=owner_auth, json={"proof": forged})
        assert invalid.status_code == 401, invalid.text
        assert invalid.json()["error"]["details"]["reason"] == "proof_invalid"
        response = await client.post(path, headers=owner_auth, json={"proof": stale})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["details"]["reason"] == "challenge_stale"
        assert (await client.get("/api/box/account", headers=owner_auth)).status_code == 200
        assert (await client.get("/api/box/account", headers=other_auth)).status_code == 200

    fresh = await _action(client, owner_key, owner_id, "revoke", params)
    response = await client.post(path, headers=owner_auth, json={"proof": fresh})
    assert response.status_code == 204, response.text
    assert (await client.get("/api/box/account", headers=owner_auth)).status_code == 200
    assert (await client.get("/api/box/account", headers=other_auth)).status_code == 401


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
    assert "/api/box/primary-claims" not in paths
    for path, method in [
        ("/api/box/status", "get"),
        ("/api/box/pairings", "post"),
        ("/api/box/owner-claims", "post"),
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
    from inhere_box.api.common.box_auth import BOX_CSRF_HEADER, browser_cookie_name

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
        ("/api/box/clients/{client_id}/primary", "post"),
        ("/api/box/clients/{client_id}/owner", "post"),
        ("/api/box/management-key/rotate", "post"),
    ]:
        operation = paths[path][method]
        assert operation["security"] == [{"BoxAdminCookie": [], "BoxCSRF": []}]
        origin = next(p for p in operation["parameters"] if p["name"] == "Origin")
        assert origin["required"] is True
    for path in ["/api/box/pairing-grants", "/api/box/clients/{client_id}/revoke"]:
        operation = paths[path]["post"]
        assert operation["security"] == [{"BearerAuth": []}, {"BoxAdminCookie": [], "BoxCSRF": []}]
        origin = next(p for p in operation["parameters"] if p["name"] == "Origin")
        assert origin["required"] is False
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


async def test_box_http_is_available_without_forwarded_headers_or_whitelist(box_env) -> None:
    client, _server, _home = box_env
    app = client._inhere_box_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.20", 1234)),
        base_url="http://127.0.0.1",
    ) as external:
        response = await external.get("/api/box/status")
        assert response.status_code == 200, response.text
        response = await external.get(
            "/api/box/status",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 200, response.text
        assert (await external.get("/api/projects")).status_code == 401


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
        transport=httpx.ASGITransport(app=client._inhere_box_app, client=("198.51.100.20", 1234)),
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


async def test_trusted_cli_key_initialization_and_rotation_require_stopped_daemon(
    tmp_inhere_box_home,
):
    import asyncio
    import re

    from click.testing import CliRunner

    from inhere_box.cli.main import cli

    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    runner = CliRunner()
    async with inhere_box_client(tmp_inhere_box_home) as (_client, _server):
        refused = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
        assert refused.exit_code != 0 and not refused.stdout
    initialized = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
    assert initialized.exit_code == 0
    management_key = initialized.stdout.strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", management_key)
    assert initialized.stdout == management_key + "\n" and management_key not in initialized.stderr
    duplicate = await asyncio.to_thread(runner.invoke, cli, ["box", "key", "init"])
    assert duplicate.exit_code != 0 and not duplicate.stdout
    async with inhere_box_client(tmp_inhere_box_home) as (client, _server):
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
    async with inhere_box_client(tmp_inhere_box_home) as (client, _server):
        client.base_url = httpx.URL("http://localhost")
        for candidate, expected in [(management_key, 401), (new_key, 200)]:
            response = await client.post(
                "/api/auth/browser/sessions",
                headers={"Origin": "http://localhost"},
                json={"management_key": candidate},
            )
            assert response.status_code == expected, response.text


async def test_delete_revoked_device_requires_web_admin_and_csrf(box_env):
    client, server, _home = box_env
    _key, client_id, device_auth = await _claim(client, server)
    path = f"/api/box/clients/{client_id}"
    assert (await client.delete(path, headers=device_auth)).status_code == 403
    admin_headers = await _admin_login(client, server)
    assert (await client.delete(path, headers=admin_headers)).status_code == 409
    assert (await client.post(f"{path}/revoke", headers=admin_headers, json={})).status_code == 204
    assert (await client.delete(path, headers={"Origin": "http://localhost"})).status_code == 403
    assert (
        await client.delete(path, headers={**admin_headers, "Origin": "http://other"})
    ).status_code == 403
    response = await client.delete(path, headers=admin_headers)
    assert response.status_code == 204 and not response.content
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/box/clients", headers=admin_headers)).json() == []
    assert (await client.get("/api/box/account", headers=admin_headers)).status_code == 200
    assert (await client.delete(path, headers=admin_headers)).status_code == 404
    schema = (await client.get("/api/openapi.json")).json()
    assert "204" in schema["paths"]["/api/box/clients/{client_id}"]["delete"]["responses"]
    assert (await client.get("/api/docs")).status_code == 200


async def test_manual_login_reuses_device_then_web_revoke_exposes_signed_status(box_env):
    from inhere_box.infra.auth.box_accounts.crypto import digest

    client, server, _home = box_env
    key, client_id, auth = await _claim(client, server)
    management_key = await _management_key(server)
    proof = await _action(client, key, client_id, "login", {"secret_hash": digest(management_key)})
    denied = await client.post("/api/auth/client-logins", json={"proof": proof, "secret": "wrong"})
    assert denied.status_code == 401
    response = await client.post(
        "/api/auth/client-logins", json={"proof": proof, "secret": management_key}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["paired_client_id"] == client_id
    assert (
        await client.post(
            "/api/auth/client-logins", json={"proof": proof, "secret": management_key}
        )
    ).status_code == 401
    admin_headers = await _admin_login(client, server)
    assert len((await client.get("/api/box/clients", headers=admin_headers)).json()) == 1
    assert (
        await client.post(f"/api/box/clients/{client_id}/revoke", headers=admin_headers, json={})
    ).status_code == 204
    assert (await client.get("/api/box/account", headers=auth)).status_code == 401
    client.cookies.clear()
    proof = await _action(client, key, client_id, "client-status")
    status = await client.post(f"/api/box/clients/{client_id}/status", json={"proof": proof})
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "revoked" and status.json()["revoked_at"] is not None
    assert status.headers["cache-control"] == "no-store"
    assert (
        await client.post(f"/api/box/clients/{client_id}/status", json={"proof": proof})
    ).status_code == 401
    schema = (await client.get("/api/openapi.json")).json()["paths"]
    for path in ["/api/auth/client-logins", "/api/box/clients/{client_id}/status"]:
        assert not schema[path]["post"].get("security")
    proof = await _action(client, key, client_id, "login", {"secret_hash": digest(management_key)})
    restored = await client.post(
        "/api/auth/client-logins", json={"proof": proof, "secret": management_key}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["paired_client_id"] == client_id
    assert (await client.get("/api/box/account", headers=auth)).status_code == 401
    restored_auth = {"Authorization": "Bearer " + restored.json()["access_token"]}
    assert (await client.get("/api/box/account", headers=restored_auth)).status_code == 200


async def test_manual_guest_http_login_preserves_account_and_consumes_share(box_env):
    from inhere_box.infra.auth.box_accounts.crypto import digest

    client, server, _home = box_env
    owner_key, owner_id, owner_auth = await _claim(client, server)
    issue = await _action(client, owner_key, owner_id, "enroll", {"request_id": "initial-guest"})
    grant = (
        await client.post(
            "/api/box/pairing-grants",
            headers=owner_auth,
            json={"request_id": "initial-guest", "proof": issue},
        )
    ).json()
    key = ec.generate_private_key(ec.SECP256R1())
    candidate = await _candidate(client, grant, key, request_id="guest-login")
    client_id = candidate["client_id"]
    guest_auth, _ = await _login(client, key, client_id)
    original = (await client.get("/api/box/account", headers=guest_auth)).json()
    admin_headers = await _admin_login(client, server)
    assert (
        await client.post(f"/api/box/clients/{client_id}/revoke", headers=admin_headers, json={})
    ).status_code == 204
    issue = await _action(client, owner_key, owner_id, "enroll", {"request_id": "guest-reconnect"})
    grant = (
        await client.post(
            "/api/box/pairing-grants",
            headers=owner_auth,
            json={"request_id": "guest-reconnect", "proof": issue},
        )
    ).json()
    client.cookies.clear()
    proof = await _action(client, key, client_id, "login", {"secret_hash": digest(grant["secret"])})
    response = await client.post(
        "/api/auth/client-logins", json={"proof": proof, "secret": grant["secret"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "guest" and response.json()["paired_client_id"] == client_id
    auth = {"Authorization": "Bearer " + response.json()["access_token"]}
    account = (await client.get("/api/box/account", headers=auth)).json()
    assert account["account_id"] == original["account_id"] and account["user"] == original["user"]
    proof = await _action(client, key, client_id, "login", {"secret_hash": digest(grant["secret"])})
    assert (
        await client.post(
            "/api/auth/client-logins", json={"proof": proof, "secret": grant["secret"]}
        )
    ).status_code == 401
    admin_headers = await _admin_login(client, server)
    assert len((await client.get("/api/box/clients", headers=admin_headers)).json()) == 2
