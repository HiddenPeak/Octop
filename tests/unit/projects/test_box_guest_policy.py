"""Sharing a client must not grant the box host's execution or data authority."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import box_guest_policy, engine_catalog, service, tasks
from octop.infra.projects.models import ProjectFields
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    db = SqlitePool(tmp_path / "box-guests.db")
    run_migrations(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','admin',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'guest','user',1)")
    try:
        yield SharedServices(
            PathLayout(tmp_path), OctopConfig(account_mode="box_local"), RepoBundle.from_pool(db)
        )
    finally:
        db.close()


def test_guest_projects_have_independent_ownership_and_cannot_mount_host(services, tmp_path):
    owner = service.create_project(services, 1, ProjectFields(name="Private"), "owner-project")
    guest = service.create_project(services, 2, ProjectFields(name="Guest"), "guest-project")
    assert [p.project_id for p in service.list_projects(services, 2)] == [guest.project_id]
    with pytest.raises(OctopError) as error:
        service.get_project(services, 2, owner.project_id)
    assert error.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(OctopError) as error:
        service.create_project(
            services, 2, ProjectFields(name="Host", directory=str(tmp_path)), "host-project"
        )
    assert error.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "field,value",
    [
        ("plugins", ["installed-plugin"]),
        ("mcp_servers", ["owner-connector"]),
        ("subagents", ["code"]),
        ("knowledge_base_ids", ["shared-kb"]),
        ("skills", [{"package_id": "installed", "slug": "read-host"}]),
    ],
)
def test_guest_task_overrides_cannot_enable_host_resources(services, field, value):
    with pytest.raises(OctopError) as error:
        tasks._base_configuration(services, 2, None, tasks.TaskExecutionOptions(**{field: value}))
    assert error.value.code == ErrorCode.FORBIDDEN


def test_guest_catalog_and_default_snapshot_are_scoped(services):
    resources = tasks.resources(services, 2)
    assert resources.directory_roots == []
    assert resources.experts == []
    assert resources.skills == []
    assert resources.plugins == []
    assert resources.mcp_servers == []
    assert [entry.engine.engine_id for entry in resources.execution_engines] == ["octop"]
    snapshot = tasks._base_configuration(services, 2, None, tasks.TaskExecutionOptions())
    assert snapshot["directory"] is None
    assert snapshot["knowledge_base_ids"] == []
    with pytest.raises(OctopError) as error:
        engine_catalog.validate_engine(
            services, 2, engine_catalog.EngineSelection(engine_id="acp", connection_id="runner")
        )
    assert error.value.code == ErrorCode.TASK_SETTINGS_INVALID


@pytest.mark.parametrize(
    "configuration",
    [
        {"directory": "host-directory"},
        {"execution_engine": {"engine_id": "acp"}},
        {"expert_profile": {"expert_id": "owner-expert"}},
        {"execution_kind": "resident"},
    ],
)
def test_queued_runtime_configuration_is_revalidated(services, configuration):
    with pytest.raises(OctopError):
        box_guest_policy.validate_configuration(services, 2, configuration)
    box_guest_policy.validate_configuration(services, 1, configuration)
    legacy = replace(services, config=replace(services.config, account_mode="server"))
    box_guest_policy.validate_configuration(legacy, 2, configuration)


@pytest.mark.parametrize("name", ["execute", "browser_use", "web_fetch", "task", "unknown_tool"])
async def test_guest_denies_host_or_unknown_tool_before_dispatch(name):
    handler = AsyncMock()
    request = ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )
    result = await box_guest_policy.BoxGuestToolsMiddleware(
        {"client__own_tool"}, "en"
    ).awrap_tool_call(request, handler)
    assert result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.parametrize("name", ["read_file", "write_file", "client__own_tool"])
async def test_guest_own_workspace_and_bound_client_tools_remain_available(name):
    handler = AsyncMock(return_value=ToolMessage(content="ok", tool_call_id="call"))
    request = ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )
    result = await box_guest_policy.BoxGuestToolsMiddleware(
        {"client__own_tool"}, "en"
    ).awrap_tool_call(request, handler)
    assert result.content == "ok"
    handler.assert_awaited_once()


async def test_box_boot_does_not_create_residents_for_invited_users(services, monkeypatch):
    from octop.infra.users.manager import UserManager

    manager = UserManager(services)
    ensure = AsyncMock()
    monkeypatch.setattr(manager, "ensure_resident", ensure)
    await manager.boot()
    ensure.assert_awaited_once_with(1)
