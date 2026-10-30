"""Box device authorization must not become a background task's session lifetime."""

from __future__ import annotations

import json

from octop.infra.auth.box_context import box_request_auth
from octop.infra.projects import client_tools, runtime
from octop.infra.projects.client_tool_models import ClientToolDefinition
from tests.integration.test_box_account_connections import (
    _command,
    _connect,
    _ready,
    _second_client,
    connected_box,  # noqa: F401 — pytest fixture
)
from tests.integration.test_project_task_client_tools import DEFINITION, identity
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import until
from tests.support.auth import seed_openai_provider
from tests.support.box_accounts import _login


async def test_box_tool_worker_survives_logout_and_requires_the_original_device(
    connected_box,  # noqa: F811
    monkeypatch,
):
    from octop.api.routers import project_tasks_ws

    client, _server, key, client_id, auth = connected_box
    await seed_openai_provider(client, auth)
    _other_key, other_id, other_auth = await _second_client(client, key, client_id, auth)
    observed = []

    async def run(services, owner, turn, history):
        # A real worker is started at server boot, outside all authenticated requests.
        assert box_request_auth.get() is None
        definition = ClientToolDefinition.model_validate(DEFINITION)
        broker = client_tools.ClientToolBroker(services, owner, turn, client_id, [definition])
        output = await broker.invoke(definition, {"path": "note.md"})
        observed.append(json.loads(output))
        yield {"type": "token", "content": "Read the client note"}

    monkeypatch.setattr(runtime, "run_turn", run)
    monkeypatch.setattr(client_tools, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    async with _connect(client, auth) as socket:
        await _ready(socket)
        assert (await _command(socket, "client.attach", "attach", client_id=client_id))[
            "type"
        ] == "result"
        created = await _command(
            socket,
            "task.create",
            "create",
            data={
                "request_id": "box-client-note",
                "prompt": "Read my note",
                "options": {
                    "client_id": client_id,
                    "client_tools": [DEFINITION],
                    "action_timeline_enabled": False,
                },
            },
        )
        assert created["type"] == "result", created
        call = (await until(socket, lambda frame: frame["type"] == "tool.call"))["data"]
        assert call["status"] == "pending"
        assert (await client.post("/api/auth/logout", headers=auth)).status_code == 204

    # An independent guest cannot read or execute the primary device's call.
    async with _connect(client, other_auth) as other:
        await _ready(other)
        await _command(other, "client.attach", "attach-other", client_id=other_id)
        denied = await _command(other, "tool.get", "wrong-device", **identity(call))
        assert denied["type"] == "error" and denied["status"] in {403, 404}

    # Signing in again on the original device resumes the pending handoff.
    renewed, _ = await _login(client, key, client_id)
    async with _connect(client, renewed) as resumed:
        await _ready(resumed)
        await _command(resumed, "client.attach", "attach-again", client_id=client_id)
        claimed = await _command(
            resumed, "tool.claim", "claim", **identity(call), request_id="box-note-claim"
        )
        assert claimed["type"] == "result", claimed
        completed = await _command(
            resumed,
            "tool.result",
            "complete",
            **identity(call),
            data={"claim_token": claimed["data"]["claim_token"], "output": "the note"},
        )
        assert completed["type"] == "result", completed
    done = await settled(client, renewed, created["data"]["task_id"])
    assert done["status"] == "completed"
    assert observed == [{"output": "the note", "is_error": False}]
