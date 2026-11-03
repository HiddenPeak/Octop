"""Typed context usage and explicit compaction for private resident conversations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from inhere_box.api.deps import current_user, get_server
from inhere_box.infra.agents.resident_context import (
    ResidentContextCompact,
    ResidentContextUsage,
    compact_context,
    context_usage,
)
from inhere_box.infra.agents.resident_conversation import ResidentConversationRuntime
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.server import InhereBoxServer
from inhere_box.infra.users.identity import User

router = APIRouter(prefix="/agents/{agent_id}/threads/{thread_id}/conversation")


def _runtime(server: InhereBoxServer) -> ResidentConversationRuntime:
    runtime = server.app_runtime.resident_conversations if server.app_runtime else None
    if runtime is None:
        raise InhereBoxError.localized(ErrorCode.SETUP_REQUIRED)
    return runtime


@router.get(
    "/context",
    response_model=ResidentContextUsage,
    summary="Read private conversation context usage",
)
async def usage(
    agent_id: str,
    thread_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentContextUsage:
    response.headers["Cache-Control"] = "no-store"
    return await context_usage(_runtime(server), user.id, agent_id, thread_id)


@router.post(
    "/context:compact",
    response_model=ResidentContextCompact,
    summary="Explicitly compact one idle private conversation's model context",
    description="Preserves full archived history and the original thread. Each call is a new explicit maintenance operation. A disconnected caller does not interrupt checkpoint settlement. No client-selected model or tools are accepted.",
)
async def compact(
    agent_id: str,
    thread_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentContextCompact:
    response.headers["Cache-Control"] = "no-store"
    return await compact_context(_runtime(server), user.id, agent_id, thread_id)
