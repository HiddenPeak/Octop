"""Exercise box authentication on actual task WebSocket handlers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.websockets import WebSocketDisconnect

from inhere_box.infra.auth.tokens import sign_token
from inhere_box.infra.server import InhereBoxServer
from tests.support.app import inhere_box_client, write_inhere_box_config
from tests.support.box_accounts import _action, _admin_login, _candidate, _claim, _login
from tests.support.http import ASGIWebSocketSession


class BoxSocket(ASGIWebSocketSession):
    def _scope(self) -> dict[str, Any]:
        scope = super()._scope()
        scope["scheme"] = "wss"
        return scope


@asynccontextmanager
async def _connect(
    client: httpx.AsyncClient,
    auth: dict[str, str] | None = None,
    *,
    query: str = "",
    path: str = "/api/project-tasks/ws",
) -> AsyncIterator[BoxSocket]:
    socket = BoxSocket(client._inhere_box_app, path + query, headers=auth)
    try:
        await socket.connect(timeout=3)
        yield socket
    finally:
        await socket.close()


@pytest.fixture
async def connected_box(
    tmp_inhere_box_home: Path,
) -> AsyncIterator[
    tuple[httpx.AsyncClient, InhereBoxServer, ec.EllipticCurvePrivateKey, str, dict[str, str]]
]:
    write_inhere_box_config(tmp_inhere_box_home, account_mode="box_local")
    async with inhere_box_client(tmp_inhere_box_home) as (client, server):
        key, client_id, auth = await _claim(client, server)
        yield client, server, key, client_id, auth


async def _second_client(client, first_key, first_id, first_auth):
    proof = await _action(client, first_key, first_id, "enroll", {"request_id": "second"})
    grant = await client.post(
        "/api/box/pairing-grants",
        headers=first_auth,
        json={"request_id": "second", "proof": proof},
    )
    assert grant.status_code == 200, grant.text
    key = ec.generate_private_key(ec.SECP256R1())
    candidate = await _candidate(client, grant.json(), key, request_id="second", name="Second")
    client_id = candidate["client_id"]
    auth, _ = await _login(client, key, client_id)
    return key, client_id, auth


async def _ready(socket: BoxSocket) -> None:
    assert await socket.receive_json(timeout=3) == {
        "type": "ready",
        "protocol_version": 3,
        "session_replace": True,
    }


async def _command(socket: BoxSocket, kind: str, key: str, **fields: Any) -> dict[str, Any]:
    key = "command-" + key
    await socket.send_json({"type": kind, "id": key, **fields})
    for _ in range(10):
        frame = await socket.receive_json(timeout=3)
        if frame.get("id") == key:
            return frame
    pytest.fail("task command response not received")


async def _closed(socket: BoxSocket) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            await socket.receive_json(timeout=3)
    assert closed.value.code == 4401


async def test_box_ws_requires_header_and_rejects_legacy_or_query_tokens(connected_box) -> None:
    client, server, _key, _client_id, auth = connected_box
    assert server.services is not None
    account = (await client.get("/api/box/account", headers=auth)).json()
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    legacy = sign_token(secret, sub=account["owner"]["id"], uname="owner", role="admin")
    token = auth["Authorization"].split(" ", 1)[1]
    cases = [
        (None, ""),
        ({"Authorization": f"Bearer {legacy}"}, ""),
        (None, "?token=" + token),
        (auth, "?token=" + token),
        (auth, "?access_token=" + token),
    ]
    for headers, query in cases:
        with pytest.raises(WebSocketDisconnect) as closed:
            async with _connect(client, headers, query=query):
                pytest.fail("invalid box credentials completed the handshake")
        assert closed.value.code == 4401
    async with _connect(client, auth) as socket:
        await _ready(socket)
        result = await _command(socket, "ping", "live")
        assert result["type"] == "result"


async def test_native_socket_replaces_only_the_same_devices_session(connected_box) -> None:
    client, server, key, client_id, original = connected_box
    _, _, other = await _second_client(client, key, client_id, original)
    renewed, receipt = await _login(client, key, client_id)
    assert receipt["account"] == (await client.get("/api/box/account", headers=renewed)).json()
    assert server.box_account_service is not None and server.services is not None
    old = server.box_account_service.validate_access_token(
        original["Authorization"].split(" ", 1)[1]
    )

    async with _connect(client, original) as socket:
        await _ready(socket)
        denied = await _command(
            socket,
            "session.replace",
            "cross-device",
            access_token=other["Authorization"].split(" ", 1)[1],
        )
        assert denied["type"] == "error" and denied["status"] == 403
        assert (
            await _command(
                socket, "session.replace", "same-device", access_token=receipt["access_token"]
            )
        )["type"] == "result"
        with server.services.db.transaction() as conn:
            server.services.box_account_repo.revoke_session(conn, old.session_id, int(time.time()))
        # Domain authorization and the middleware's continuous check must
        # both use the replacement, on the existing connection.
        assert (await _command(socket, "client.attach", "after-rotation", client_id=client_id))[
            "type"
        ] == "result"
        with server.services.db.transaction() as conn:
            server.services.box_account_repo.revoke_session(
                conn, receipt["session_id"], int(time.time())
            )
        await _closed(socket)


async def test_revoke_closes_existing_client_socket_and_preserves_other_client(
    connected_box,
) -> None:
    client, server, first_key, first_id, first_auth = connected_box
    second_key, second_id, second_auth = await _second_client(
        client, first_key, first_id, first_auth
    )
    async with _connect(client, first_auth) as first, _connect(client, second_auth) as second:
        await _ready(first)
        await _ready(second)
        spoof = await _command(first, "client.attach", "spoof", client_id=second_id)
        assert spoof["type"] == "error" and spoof["status"] == 403
        assert spoof["error"]["details"]["reason"] == "box_client_mismatch"
        own = await _command(first, "client.attach", "own", client_id=first_id)
        assert own["type"] == "result"
        other = await _command(second, "client.attach", "other", client_id=second_id)
        assert other["type"] == "result"
        proof = await _action(
            client, first_key, first_id, "revoke", {"target_client_id": second_id}
        )
        revoked = await client.post(
            f"/api/box/clients/{second_id}/revoke", headers=first_auth, json={"proof": proof}
        )
        assert revoked.status_code == 204, revoked.text
        # Do not send more frames: idle transports must be actively closed too.
        await _closed(second)
        assert (await _command(first, "ping", "still-live"))["type"] == "result"
        assert (await client.get("/api/box/account", headers=first_auth)).status_code == 200


async def test_logout_closes_every_socket_for_sid_but_preserves_another_session(
    connected_box,
) -> None:
    client, _server, key, client_id, auth = connected_box
    other_auth, _ = await _login(client, key, client_id)
    async with (
        _connect(client, auth) as one,
        _connect(client, auth) as two,
        _connect(client, other_auth) as other,
    ):
        await _ready(one)
        await _ready(two)
        await _ready(other)
        response = await client.post("/api/auth/logout", headers=auth)
        assert response.status_code == 204
        await asyncio.gather(_closed(one), _closed(two))
        assert (await _command(other, "ping", "same-client-other-session"))["type"] == "result"
        assert (await client.get("/api/box/account", headers=auth)).status_code == 401
        assert (await client.get("/api/box/account", headers=other_auth)).status_code == 200
        clients = (await client.get("/api/box/clients", headers=other_auth)).json()
        assert clients[0]["state"] == "active"


async def test_revoked_request_cannot_admit_task_after_waiting_outside_transaction(
    connected_box, monkeypatch
) -> None:
    import threading

    from inhere_box.api.middleware import box_accounts
    from inhere_box.infra.projects import task_submission

    client, _server, key, client_id, auth = connected_box
    other_auth, _ = await _login(client, key, client_id)
    waiting, release = threading.Event(), threading.Event()
    original = task_submission._send_with_hook

    def delayed(*args, **kwargs):
        waiting.set()
        assert release.wait(timeout=5), "test did not release task admission"
        return original(*args, **kwargs)

    monkeypatch.setattr(task_submission, "_send_with_hook", delayed)
    # Let domain admission provide the rejection, without the transport monitor
    # cancelling the request before its background thread resumes.
    monkeypatch.setattr(box_accounts, "_RECHECK_SECONDS", 60)
    pending = asyncio.create_task(
        client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "delayed-admission", "prompt": "Must not be accepted"},
        )
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 3)
        assert (await client.post("/api/auth/logout", headers=auth)).status_code == 204
        release.set()
        response = await asyncio.wait_for(pending, 3)
        assert response.status_code == 401, response.text
        listing = await client.get("/api/project-tasks", headers=other_auth)
        assert listing.status_code == 200, listing.text
        assert listing.json()["items"] == []
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_notifications_accept_primary_header_reject_guest_and_close_on_revoke(
    connected_box,
) -> None:
    client, server, first_key, first_id, first_auth = connected_box
    _second_key, _second_id, second_auth = await _second_client(
        client, first_key, first_id, first_auth
    )
    with pytest.raises(WebSocketDisconnect) as denied:
        async with _connect(client, second_auth, path="/api/notifications/ws"):
            pytest.fail("guest opened owner notification stream")
    assert denied.value.code == 4401
    async with _connect(client, first_auth, path="/api/notifications/ws") as first:
        await first.send_json({"type": "ping"})
        assert await first.receive_json(timeout=3) == {"type": "pong"}
        admin = await _admin_login(client, server)
        response = await client.post(f"/api/box/clients/{first_id}/revoke", headers=admin, json={})
        assert response.status_code == 204, response.text
        await _closed(first)
        assert (await client.get("/api/box/account", headers=second_auth)).status_code == 200


@pytest.mark.parametrize("surface", ["terminal", "browser-stream"])
async def test_task_workbench_header_auth_rechecks_actions_and_closes_on_logout(
    connected_box, monkeypatch, surface
) -> None:
    from urllib.parse import urlencode

    from inhere_box.api.routers import project_task_workbench as workbench
    from inhere_box.infra.projects import runtime
    from tests.support.auth import seed_openai_provider

    client, _server, _key, _client_id, auth = connected_box
    await seed_openai_provider(client, auth)

    async def finish_turn(*args):
        yield {"type": "token", "content": "ready"}

    monkeypatch.setattr(runtime, "run_turn", finish_turn)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "workbench-box-session", "prompt": "Prepare workspace"},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    for _ in range(100):
        task = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        if task["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert task["status"] == "completed"
    view = await client.get(f"/api/project-tasks/{task_id}/workbench", headers=auth)
    assert view.status_code == 200, view.text
    finished = asyncio.Event()

    async def fake_terminal(websocket, *args, validate, **kwargs):
        await websocket.accept()
        try:
            await validate()
            await websocket.send_json({"type": "attached"})
            while True:
                await websocket.receive_text()
                await validate()
                await websocket.send_json({"type": "action"})
        finally:
            finished.set()

    async def fake_browser(**options):
        try:
            await options["send_json"]({"type": "attached"})
            while True:
                await options["receive_text"]()
                await options["send_json"]({"type": "action"})
        finally:
            finished.set()

    monkeypatch.setattr(workbench.terminal, "run_terminal_session", fake_terminal)
    monkeypatch.setattr(workbench, "run_browser_stream_session", fake_browser)
    path = f"/api/project-tasks/{task_id}/{surface}/ws"
    query = "?" + urlencode({"expected_scope_revision": view.json()["scope_revision"]})
    async with _connect(client, auth, path=path, query=query) as socket:
        assert await socket.receive_json(timeout=3) == {"type": "attached"}
        await socket.send_json({"type": "input"})
        assert await socket.receive_json(timeout=3) == {"type": "action"}
        response = await client.post("/api/auth/logout", headers=auth)
        assert response.status_code == 204, response.text
        await _closed(socket)
        await asyncio.wait_for(finished.wait(), 3)
