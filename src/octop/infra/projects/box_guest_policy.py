"""Limit invited box clients to their own task data and client-side tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Set
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from octop.i18n import tr
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError


def is_guest(services: SharedServices, owner_id: int) -> bool:
    if services.config.account_mode != "box_local":
        return False
    user = services.user_repo.get(owner_id)
    return user is None or user.role != "admin"


def validate_configuration(
    services: SharedServices, owner_id: int, configuration: Mapping[str, Any]
) -> None:
    if not is_guest(services, owner_id):
        return
    if (
        any(
            configuration.get(name)
            for name in (
                "directory",
                "skills",
                "mcp_servers",
                "plugins",
                "subagents",
                "team_enabled",
                "selected_expert_id",
                "expert_profile",
                "knowledge_base_ids",
                "source_origin",
                "execution_kind",
            )
        )
        or (configuration.get("execution_engine") or {}).get("engine_id", "octop") != "octop"
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_guest_resource"})


class BoxGuestToolsMiddleware(AgentMiddleware[Any, Any]):
    """Filter model tools and reject restored calls outside the guest tool set.

    Task filesystem tools use the task's virtual workspace. Shells, browsers,
    remote fetch, plugins and subagents would escape that scope, so are absent.
    Client tool names are passed only after the normal authenticated-device bind.
    """

    def __init__(self, task_tools: Set[str], locale: str) -> None:
        self.allowed = task_tools | {
            "read_file",
            "write_file",
            "edit_file",
            "ls",
            "glob",
            "grep",
            "memory_search",
            "memory_get",
        }
        self.locale = locale

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        return await handler(
            request.override(
                tools=[
                    tool
                    for tool in request.tools
                    if (tool.get("name", "") if isinstance(tool, dict) else tool.name)
                    in self.allowed
                ]
            )
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if request.tool_call["name"] not in self.allowed:
            return ToolMessage(
                content=tr("errors.FORBIDDEN", self.locale),
                name=request.tool_call["name"],
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        return await handler(request)
