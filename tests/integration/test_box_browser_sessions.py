"""Browser cookies, CSRF, session replacement and real task WebSockets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.websockets import WebSocketDisconnect

from inhere_box.api.common.box_auth import browser_cookie_name
from tests.support.app import inhere_box_client, write_inhere_box_config
from tests.support.box_accounts import _claim, _management_key
from tests.support.http import ASGIWebSocketSession

ORIGIN = "https://box.test"


@pytest.fixture
async def browser_box(tmp_inhere_box_home: Path) -> AsyncIterator[tuple[Any, ...]]:
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        client.base_url = httpx.URL(ORIGIN)
        key, client_id, native_auth = await _claim(client, server)
        yield client, server, key, client_id, native_auth


async def _browser_login(client, key, client_id, *, csrf: str | None = None):
    management_key = await _management_key(client._inhere_box_app.state.inhere_box_server)
    headers = {"Origin": str(client.base_url).rstrip("/")}
    if csrf is not None:
        headers["X-Inhere-Box-CSRF"] = csrf
    response = await client.post(
        "/api/auth/browser/sessions", headers=headers, json={"management_key": management_key}
    )
    assert response.status_code == 200, response.text
    return response


async def test_cookie_is_httponly_scoped_and_reloads_without_exposing_jwt(browser_box) -> None:
    client, server, key, client_id, _native = browser_box
    response = await _browser_login(client, key, client_id)
    body = response.json()
    assert set(body) == {
        "session_id",
        "paired_client_id",
        "expires_in",
        "csrf_token",
        "role",
        "session_kind",
    }
    assert body["paired_client_id"] is None and body["expires_in"] == 86400
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("inhere_box_session_")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/api" in cookie and "Max-Age=86400" in cookie and "Domain=" not in cookie
    assert server.box_account_service is not None
    assert browser_cookie_name(server.box_account_service.box_id) in cookie
    assert browser_cookie_name("another-box") not in cookie
    assert response.headers["cache-control"] == "no-store"

    reloaded = await client.get("/api/auth/browser/session", headers={"Origin": ORIGIN})
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["session_id"] == body["session_id"]
    assert reloaded.json()["csrf_token"] == body["csrf_token"]
    assert 0 < reloaded.json()["expires_in"] <= 86400
    # Exercise the ordinary JWT gate and current_user dependency as well.
    for path in ["/api/auth/me", "/api/projects", "/api/box/account"]:
        response = await client.get(path, headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "no-store"
        assert "x-inhere-box-access-token" not in response.headers
    assert (await client.get("/api/auth/browser/session")).status_code == 403
    response = await client.get(
        "/api/auth/browser/session", headers={"Referer": ORIGIN + "/projects"}
    )
    assert response.status_code == 200
    for origin in ["https://attacker.test", "https://box.test:8443", "null", ORIGIN + "/"]:
        response = await client.get(
            "/api/auth/browser/session",
            headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 403, response.text


async def test_cookie_mutations_require_origin_and_session_bound_csrf(browser_box) -> None:
    client, _server, key, client_id, native_auth = browser_box
    info = (await _browser_login(client, key, client_id)).json()
    for headers in [
        {},
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-Inhere-Box-CSRF": "0" * 64},
        {"X-Inhere-Box-CSRF": info["csrf_token"], "Sec-Fetch-Site": "same-origin"},
        {"Origin": "https://attacker.test", "X-Inhere-Box-CSRF": info["csrf_token"]},
    ]:
        denied = await client.post("/api/auth/logout", headers=headers)
        assert denied.status_code == 403, denied.text
    # A supplied Authorization header wins; an invalid one cannot fall back.
    denied = await client.get(
        "/api/box/account", headers={"Authorization": "Bearer invalid", "Origin": ORIGIN}
    )
    assert denied.status_code == 401
    denied = await client.get(
        "/api/box/account", headers={"Authorization": "Basic invalid", "Origin": ORIGIN}
    )
    assert denied.status_code == 401
    assert (await client.get("/api/box/account", headers=native_auth)).status_code == 200
    signed_out = await client.post(
        "/api/auth/logout",
        headers={"Origin": ORIGIN, "X-Inhere-Box-CSRF": info["csrf_token"]},
    )
    assert signed_out.status_code == 204, signed_out.text
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert not client.cookies
    assert (await client.get("/api/box/account", headers=native_auth)).status_code == 200


async def test_browser_refresh_replaces_only_previous_session_and_rotates_csrf(browser_box) -> None:
    client, server, key, client_id, native_auth = browser_box
    first = (await _browser_login(client, key, client_id)).json()
    assert server.box_account_service is not None
    name = browser_cookie_name(server.box_account_service.box_id)
    old_token = client.cookies[name]
    denied = await client.post("/api/auth/browser/refresh", headers={"Origin": ORIGIN}, json={})
    assert denied.status_code == 403
    response = await client.post(
        "/api/auth/browser/refresh",
        headers={"Origin": ORIGIN, "X-Inhere-Box-CSRF": first["csrf_token"]},
        json={},
    )
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["session_id"] != first["session_id"]
    assert second["csrf_token"] != first["csrf_token"]
    assert (
        await client.get("/api/box/account", headers={"Authorization": "Bearer " + old_token})
    ).status_code == 401
    assert (await client.get("/api/box/account", headers=native_auth)).status_code == 200
    denied = await client.post(
        "/api/auth/logout", headers={"Origin": ORIGIN, "X-Inhere-Box-CSRF": first["csrf_token"]}
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["reason"] == "box_csrf_invalid"
    # A stale revoked cookie is recoverable with a new device signature.
    client.cookies.clear()
    client.cookies.set(name, old_token, domain="box.test", path="/api")
    fresh = await _browser_login(client, key, client_id)
    assert fresh.json()["session_id"] != second["session_id"]


async def test_browser_login_requires_same_origin_for_all_http_hosts(browser_box) -> None:
    client, _server, _key, _client_id, _native = browser_box
    management_key = await _management_key(_server)
    for headers in [{}, {"Origin": "https://attacker.test"}, {"Origin": "null"}]:
        response = await client.post(
            "/api/auth/browser/sessions", headers=headers, json={"management_key": management_key}
        )
        assert response.status_code == 403
    for origin in (
        "http://box.test",
        "http://192.168.1.77:8088",
        "http://octop-dhcp.local:8088",
        "http://localhost:8088",
    ):
        client.base_url = httpx.URL(origin)
        denied = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": "http://attacker.test"},
            json={"management_key": management_key},
        )
        assert denied.status_code == 403
        response = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": origin},
            json={"management_key": management_key},
        )
        assert response.status_code == 200, response.text
        assert "Secure" not in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]


class BrowserSocket(ASGIWebSocketSession):
    def _scope(self) -> dict[str, Any]:
        scope = super()._scope()
        scope["scheme"] = "wss"
        scope["server"] = ("box.test", 443)
        scope["headers"] = [
            (name, b"box.test" if name == b"host" else value) for name, value in scope["headers"]
        ]
        return scope


async def test_cookie_task_socket_checks_origin_and_closes_on_logout(browser_box) -> None:
    client, server, key, client_id, _native = browser_box
    info = (await _browser_login(client, key, client_id)).json()
    assert server.box_account_service is not None
    name = browser_cookie_name(server.box_account_service.box_id)
    cookie = f"{name}={client.cookies[name]}"
    for origin in [None, "null", "https://attacker.test", "https://box.test:444"]:
        headers = {"Cookie": cookie}
        if origin is not None:
            headers["Origin"] = origin
        socket = BrowserSocket(client._inhere_box_app, "/api/project-tasks/ws", headers=headers)
        try:
            with pytest.raises(WebSocketDisconnect) as denied:
                await socket.connect(timeout=3)
            assert denied.value.code == 4401
        finally:
            await socket.close()
    socket = BrowserSocket(
        client._inhere_box_app,
        "/api/project-tasks/ws",
        headers={"Cookie": cookie, "Origin": ORIGIN},
    )
    try:
        await socket.connect(timeout=3)
        assert await socket.receive_json(timeout=3) == {
            "type": "ready",
            "protocol_version": 3,
        }
        await socket.send_json({"type": "ping", "id": "cookie-ping"})
        result = await socket.receive_json(timeout=3)
        assert result["type"] == "result" and result["id"] == "cookie-ping"
        response = await client.post(
            "/api/auth/logout", headers={"Origin": ORIGIN, "X-Inhere-Box-CSRF": info["csrf_token"]}
        )
        assert response.status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            await socket.receive_json(timeout=3)
        assert closed.value.code == 4401
    finally:
        await socket.close()


async def test_web_does_not_claim_a_native_slot_and_management_key_remains_reusable(
    tmp_inhere_box_home,
) -> None:
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        client.base_url = httpx.URL(ORIGIN)
        await _management_key(server)
        info = (await _browser_login(client, None, None)).json()
        headers = {"Origin": ORIGIN, "X-Inhere-Box-CSRF": info["csrf_token"]}
        account = (await client.get("/api/box/account", headers=headers)).json()
        assert account["role"] == "admin" and account["primary_client_id"] is None
        assert (await client.get("/api/box/clients", headers=headers)).json() == []
        key, client_id, native = await _claim(client, server)
        assert client_id
        assert (await client.get("/api/box/account", headers=native)).json()["role"] == "primary"
        fresh = await _browser_login(client, key, client_id, csrf=info["csrf_token"])
        assert fresh.status_code == 200
        # A management JWT cannot bypass CSRF by being moved to Authorization.
        name = browser_cookie_name(server.box_account_service.box_id)
        response = await client.get(
            "/api/box/account", headers={"Authorization": "Bearer " + client.cookies[name]}
        )
        assert response.status_code == 401
        # A native JWT cannot become a Web session by being moved to Cookie.
        client.cookies.clear()
        client.cookies.set(name, native["Authorization"].split()[1], domain="box.test", path="/api")
        assert (await client.get("/api/box/account", headers={"Origin": ORIGIN})).status_code == 401


async def test_rotate_key_revokes_web_sessions_and_shares_but_keeps_native_clients(
    browser_box,
) -> None:
    client, server, key, client_id, native = browser_box
    old_key = await _management_key(server)
    info = (await _browser_login(client, key, client_id)).json()
    headers = {"Origin": ORIGIN, "X-Inhere-Box-CSRF": info["csrf_token"]}
    share = await client.post(
        "/api/box/pairing-grants", headers=headers, json={"request_id": "rotate-share"}
    )
    assert share.status_code == 200, share.text
    name = browser_cookie_name(server.box_account_service.box_id)
    old_cookie = client.cookies[name]
    rotated = await client.post(
        "/api/box/management-key/rotate", headers=headers, json={"revoke_clients": False}
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["management_key"] != old_key
    assert not client.cookies
    denied = await client.post(
        "/api/auth/browser/sessions", headers={"Origin": ORIGIN}, json={"management_key": old_key}
    )
    assert denied.status_code == 401
    client.cookies.set(name, old_cookie, domain="box.test", path="/api")
    assert (
        await client.get("/api/auth/browser/session", headers={"Origin": ORIGIN})
    ).status_code == 401
    client.cookies.clear()
    response = await client.post(
        "/api/auth/browser/sessions",
        headers={"Origin": ORIGIN},
        json={"management_key": rotated.json()["management_key"]},
    )
    assert response.status_code == 200, response.text
    assert (await client.get("/api/box/account", headers=native)).status_code == 200
    from tests.support.box_accounts import _jwk

    denied_share = await client.post(
        "/api/box/pairings",
        json={
            "secret": share.json()["secret"],
            "request_id": "rotated-share",
            "public_key_jwk": _jwk(ec.generate_private_key(ec.SECP256R1())),
            "name": "Denied",
            "platform": "test",
        },
    )
    assert denied_share.status_code in {401, 409}
