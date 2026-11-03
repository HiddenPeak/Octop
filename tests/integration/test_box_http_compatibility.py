"""Default HTTP/WS access retains authentication, origin and CSRF checks."""

from typing import Any

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from inhere_box.api.common.box_auth import browser_cookie_name
from tests.support.app import inhere_box_client, write_inhere_box_config
from tests.support.box_accounts import _claim, _management_key
from tests.support.http import ASGIWebSocketSession

ORIGIN = "http://box.test:10188"


class LanHttpBoxSocket(ASGIWebSocketSession):
    def _scope(self) -> dict[str, Any]:
        scope = super()._scope()
        scope["client"] = ("192.168.1.30", 1234)
        scope["server"] = ("192.168.1.77", 8088)
        scope["headers"] = [
            (name, b"192.168.1.77:8088" if name == b"host" else value)
            for name, value in scope["headers"]
        ]
        return scope


async def test_lan_http_tracks_new_address_and_retains_authentication(tmp_inhere_box_home):
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with (
        inhere_box_client(tmp_inhere_box_home) as (local, server),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=local._inhere_box_app, client=("192.168.1.30", 1234)),
            base_url="http://192.168.1.20:8088",
        ) as client,
    ):
        assert (await client.get("/api/box/status")).status_code == 200
        assert (await client.get("/api/projects")).status_code == 401
        _key, _client_id, auth = await _claim(client, server)
        assert (await client.get("/api/projects", headers=auth)).status_code == 200
        # A new destination address is used immediately; no config/IP list changes.
        client.base_url = "http://192.168.1.77:8088"
        assert (await client.get("/api/box/status")).status_code == 200
        assert (await client.get("/api/projects", headers=auth)).status_code == 200
        socket = LanHttpBoxSocket(local._inhere_box_app, "/api/project-tasks/ws", headers=auth)
        try:
            await socket.connect(timeout=3)
            assert (await socket.receive_json(timeout=3))["type"] == "ready"
        finally:
            await socket.close()
        client.base_url = "http://octop-dhcp.local:8088"
        assert (await client.get("/api/box/status")).status_code == 200
        assert (await client.get("/api/projects", headers=auth)).status_code == 200
        management_key = await _management_key(server)
        origin = "http://octop-dhcp.local:8088"
        denied = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": "http://attacker.test"},
            json={"management_key": management_key},
        )
        assert denied.status_code == 403
        login = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": origin},
            json={"management_key": management_key},
        )
        assert login.status_code == 200, login.text
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "Secure" not in login.headers["set-cookie"]
        assert (
            await client.get("/api/projects", headers={"Referer": origin + "/projects"})
        ).status_code == 200
        assert (
            await client.post("/api/auth/logout", headers={"Origin": origin})
        ).status_code == 403  # CSRF remains mandatory.


class HttpBoxSocket(ASGIWebSocketSession):
    def _scope(self) -> dict[str, Any]:
        scope = super()._scope()
        scope["client"] = ("198.51.100.20", 1234)
        scope["headers"] = [
            (name, b"box.test:10188" if name == b"host" else value)
            for name, value in scope["headers"]
        ]
        return scope


@pytest.mark.parametrize("origins", [[], [ORIGIN, "http://192.0.2.1:8080"]])
async def test_http_supports_native_browser_and_websocket_without_whitelist(
    tmp_inhere_box_home, origins
):
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local", box_http_origins=origins)
    async with (
        inhere_box_client(tmp_inhere_box_home) as (local, server),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=local._inhere_box_app, client=("198.51.100.20", 1234)
            ),
            base_url=ORIGIN,
        ) as client,
    ):
        assert (await client.get("/api/box/status")).status_code == 200
        assert (await client.get("/api/projects")).status_code == 401
        _key, _client_id, auth = await _claim(client, server)
        assert (await client.get("/api/projects", headers=auth)).status_code == 200
        assert (
            await client.get("http://192.0.2.1:8080/api/projects", headers=auth)
        ).status_code == 200
        management_key = await _management_key(server)
        for origin in [
            None,
            "null",
            "http://attacker.test:10188",
            "http://box.test",
            "http://192.0.2.1:8080",
        ]:
            response = await client.post(
                "/api/auth/browser/sessions",
                headers={"Origin": origin} if origin is not None else {},
                json={"management_key": management_key},
            )
            assert response.status_code == 403
        response = await client.post(
            "/api/auth/browser/sessions",
            headers={"Origin": ORIGIN},
            json={"management_key": management_key},
        )
        assert response.status_code == 200, response.text
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
        csrf = response.json()["csrf_token"]
        assert (
            await client.get("/api/projects", headers={"Referer": ORIGIN + "/projects"})
        ).status_code == 200
        assert (
            await client.get("/api/projects", headers={"Origin": "http://attacker.test"})
        ).status_code == 403
        assert (
            await client.post("/api/auth/logout", headers={"Origin": ORIGIN})
        ).status_code == 403
        name = browser_cookie_name(server.box_account_service.box_id)
        headers = {"Cookie": f"{name}={client.cookies[name]}"}
        denied = HttpBoxSocket(
            local._inhere_box_app,
            "/api/project-tasks/ws",
            headers={**headers, "Origin": "http://attacker.test"},
        )
        try:
            with pytest.raises(WebSocketDisconnect) as error:
                await denied.connect(timeout=3)
            assert error.value.code == 4401
        finally:
            await denied.close()
        socket = HttpBoxSocket(
            local._inhere_box_app, "/api/project-tasks/ws", headers={**headers, "Origin": ORIGIN}
        )
        try:
            await socket.connect(timeout=3)
            assert (await socket.receive_json(timeout=3))["type"] == "ready"
            response = await client.post(
                "/api/auth/logout", headers={"Origin": ORIGIN, "X-Inhere-Box-CSRF": csrf}
            )
            assert response.status_code == 204
            with pytest.raises(WebSocketDisconnect) as error:
                await socket.receive_json(timeout=3)
            assert error.value.code == 4401
        finally:
            await socket.close()


@pytest.mark.parametrize("configured", ["", ORIGIN])
async def test_forwarded_headers_do_not_bypass_http_authentication(tmp_inhere_box_home, configured):
    write_inhere_box_config(
        tmp_inhere_box_home,
        account_mode="box_local",
        box_http_origins=[configured] if configured else [],
    )
    async with (
        inhere_box_client(tmp_inhere_box_home) as (local, _server),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=local._inhere_box_app, client=("198.51.100.20", 1234)
            ),
            base_url=ORIGIN if not configured else "http://other.test:10188",
        ) as client,
    ):
        response = await client.get(
            "/api/projects",
            headers={
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "box.test:10188",
                "Origin": ORIGIN,
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_FAILED"
