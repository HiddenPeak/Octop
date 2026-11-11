"""Guest API allowlist, owner isolation and native role transitions."""

from uuid import uuid4

from inhere_box.infra.projects import runtime
from tests.integration.test_box_account_connections import (
    _command,
    _connect,
    _ready,
    _second_client,
    connected_box,  # noqa: F401
)
from tests.integration.test_project_tasks import settled
from tests.support.auth import seed_openai_provider
from tests.support.box_accounts import _admin_login, _login


async def test_guest_can_use_own_projects_and_tasks_but_not_owner_resources(
    connected_box,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    client, _server, key, client_id, owner_auth = connected_box
    await seed_openai_provider(client, owner_auth)

    async def finish_turn(*args):
        yield {"type": "token", "content": "Private reply"}

    monkeypatch.setattr(runtime, "run_turn", finish_turn)
    project = await client.post(
        "/api/projects",
        headers={**owner_auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Owner private"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    task = await client.post(
        f"/api/projects/{project_id}/tasks",
        headers=owner_auth,
        json={"request_id": "owner-private-task", "prompt": "Owner private history"},
    )
    assert task.status_code == 202, task.text
    task_id = task.json()["task_id"]
    await settled(client, owner_auth, task_id)
    _, _, guest = await _second_client(client, key, client_id, owner_auth)
    profile = (await client.get("/api/auth/me", headers=guest)).json()
    assert profile["role"] != "admin"
    assert (await client.get("/api/projects", headers=guest)).json() == []
    assert (await client.get("/api/project-tasks", headers=guest)).json()["items"] == []
    for path in [
        f"/api/projects/{project_id}",
        f"/api/projects/{project_id}/tasks",
        f"/api/project-tasks/{task_id}",
    ]:
        response = await client.get(path, headers=guest)
        assert response.status_code == 404, (path, response.text)
        assert "Owner private" not in response.text
    for path in [
        "/api/providers",
        "/api/agents",
        "/api/settings/captcha",
        "/api/box/clients",
        "/api/project-tasks/settings",
        "/api/project-tasks/routing/settings",
        f"/api/project-tasks/{task_id}/workbench",
        f"/api/project-tasks/{task_id}/workspace/file",
        "/api/host-directories",
    ]:
        response = await client.get(path, headers=guest)
        assert response.status_code == 403, (path, response.text)
    resources = (await client.get("/api/project-tasks/resources", headers=guest)).json()
    assert resources["models"] and resources["experts"] == []
    for field in ["directories", "skills", "mcp_servers", "plugins", "subagents"]:
        assert not resources.get(field)
    capabilities = (await client.get("/api/project-tasks/capabilities", headers=guest)).json()[
        "capabilities"
    ]
    for field in ["files", "browser", "terminal", "tool_ui", "memory", "migrations"]:
        assert capabilities[field] is False
    rejected = await client.post(
        "/api/project-tasks",
        headers=guest,
        json={
            "request_id": "guest-system-expert",
            "prompt": "Escalate",
            "selected_expert_id": "owner-expert",
        },
    )
    assert rejected.status_code == 403, rejected.text
    own = await client.post(
        "/api/projects",
        headers={**guest, "Idempotency-Key": str(uuid4())},
        json={"name": "Guest private"},
    )
    assert own.status_code == 201, own.text
    assert (
        await client.get(f"/api/projects/{own.json()['project_id']}", headers=owner_auth)
    ).status_code == 404
    unsafe = await client.post(
        "/api/projects",
        headers={**guest, "Idempotency-Key": str(uuid4())},
        json={"name": "Guest unsafe", "directory": str(tmp_path)},
    )
    assert unsafe.status_code == 403, unsafe.text
    created = await client.post(
        "/api/project-tasks",
        headers=guest,
        json={"request_id": "guest-own-task", "prompt": "Guest history"},
    )
    assert created.status_code == 202, created.text
    result = await settled(client, guest, created.json()["task_id"])
    assert result["turns"][0]["reply"] == "Private reply"
    async with _connect(client, guest) as socket:
        await _ready(socket)
        unsafe_frame = await _command(
            socket,
            "task.create",
            "guest-unsafe-engine",
            data={
                "request_id": "unsafe-engine",
                "prompt": "Escalate",
                "options": {"mcp_servers": ["private-server"]},
            },
        )
        assert unsafe_frame["type"] == "error" and unsafe_frame["status"] == 403, unsafe_frame
        own_open = await _command(
            socket, "task.open", "guest-open", task_id=result["task_id"], open_id=str(uuid4())
        )
        assert own_open["type"] == "result" and own_open["data"] is None
        denied = await _command(
            socket, "task.open", "owner-open", task_id=task_id, open_id=str(uuid4())
        )
        assert denied["type"] == "error" and denied["status"] == 404
        denied = await _command(socket, "task.open", "owner-resume", task_id=task_id, resume=True)
        assert denied["type"] == "error" and denied["status"] == 404
        denied = await _command(
            socket,
            "resident.subscribe",
            "owner-resident",
            timeline=True,
            agent_id="owner-agent",
            thread_id="owner-thread",
        )
        assert denied["type"] == "error" and denied["status"] == 403


async def test_owner_access_requires_web_admin_and_preserves_existing_owner(connected_box):  # noqa: F811
    client, server, first_key, first_id, first = connected_box
    second_key, second_id, second = await _second_client(client, first_key, first_id, first)
    assert (
        await client.post(f"/api/box/clients/{second_id}/owner", headers=second, json={})
    ).status_code == 403
    assert (
        await client.post(f"/api/box/clients/{second_id}/owner", headers=first, json={})
    ).status_code == 403
    admin = await _admin_login(client, server)
    retired = await client.post(f"/api/box/clients/{second_id}/primary", headers=admin, json={})
    assert retired.status_code == 410
    assert retired.json()["error"]["details"]["reason"] == "box_primary_transfer_retired"
    assert (await client.get("/api/box/account", headers=first)).json()["role"] == "primary"
    assert (await client.get("/api/box/account", headers=second)).json()["role"] == "guest"
    response = await client.post(f"/api/box/clients/{second_id}/owner", headers=admin, json={})
    assert response.status_code == 204, response.text
    # Only the promoted guest changes identity and must obtain a new session.
    assert (await client.get("/api/box/account", headers=first)).status_code == 200
    assert (await client.get("/api/box/account", headers=second)).status_code == 401
    renewed_first, _ = await _login(client, first_key, first_id)
    renewed_second, _ = await _login(client, second_key, second_id)
    account = (await client.get("/api/box/account", headers=renewed_first)).json()
    assert account["role"] == "primary" and account["owner"] is not None
    first_account = account
    account = (await client.get("/api/box/account", headers=renewed_second)).json()
    assert account["role"] == "primary" and account["owner"] is not None
    assert account["account_id"] == first_account["account_id"]
    assert set(account["owner_client_ids"]) == {first_id, second_id}


async def test_owner_devices_share_projects_and_history_and_revoke_independently(
    connected_box,  # noqa: F811
    monkeypatch,
):
    from cryptography.hazmat.primitives.asymmetric import ec

    from tests.support.box_accounts import _jwk, _management_key, _proof

    client, server, first_key, first_id, first = connected_box
    await seed_openai_provider(client, first)

    async def finish_turn(*args):
        yield {"type": "token", "content": "Shared owner reply"}

    monkeypatch.setattr(runtime, "run_turn", finish_turn)
    project = await client.post(
        "/api/projects",
        headers={**first, "Idempotency-Key": str(uuid4())},
        json={"name": "Shared owner project"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    task = await client.post(
        f"/api/projects/{project_id}/tasks",
        headers=first,
        json={"request_id": "shared-history", "prompt": "Remember this on my other device"},
    )
    assert task.status_code == 202, task.text
    task_id = task.json()["task_id"]
    await settled(client, first, task_id)
    second_key = ec.generate_private_key(ec.SECP256R1())
    candidate = await client.post(
        "/api/box/owner-claims",
        json={
            "management_key": await _management_key(server),
            "request_id": "second-owner",
            "public_key_jwk": _jwk(second_key),
            "name": "My laptop",
            "platform": "test",
        },
    )
    assert candidate.status_code == 200, candidate.text
    pairing = candidate.json()
    proven = await client.post(
        f"/api/box/pairings/{pairing['pairing_id']}/proof",
        json={
            "proof": _proof(second_key, pairing["challenge"]),
        },
    )
    assert proven.status_code == 200, proven.text
    second_id = proven.json()["client_id"]
    second, _ = await _login(client, second_key, second_id)
    assert (await client.get("/api/box/status")).json()["multiple_owner_devices"] is True
    first_account = (await client.get("/api/box/account", headers=first)).json()
    second_account = (await client.get("/api/box/account", headers=second)).json()
    assert first_account["account_id"] == second_account["account_id"]
    assert set(second_account["owner_client_ids"]) == {first_id, second_id}
    for path in (f"/api/projects/{project_id}", f"/api/project-tasks/{task_id}"):
        first_read = await client.get(path, headers=first)
        second_read = await client.get(path, headers=second)
        assert first_read.status_code == second_read.status_code == 200
        assert first_read.json() == second_read.json()
    _, _, guest = await _second_client(client, first_key, first_id, first)
    assert (await client.get(f"/api/project-tasks/{task_id}", headers=guest)).status_code == 404
    manager = await _admin_login(client, server)
    async with _connect(client, first) as first_ws, _connect(client, second) as second_ws:
        await _ready(first_ws)
        await _ready(second_ws)
        response = await client.post(
            f"/api/box/clients/{first_id}/revoke", headers=manager, json={}
        )
        assert response.status_code == 204
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as closed:
            await _command(first_ws, "ping", "revoked")
        assert closed.value.code == 4401
        assert (await _command(second_ws, "ping", "still-active"))["type"] == "result"
    assert (await client.get("/api/box/account", headers=first)).status_code == 401
    assert (await client.get(f"/api/project-tasks/{task_id}", headers=second)).status_code == 200
