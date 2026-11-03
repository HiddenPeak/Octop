"""Desktop environment uninstallation (SSE)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from inhere_box.api.deps import require_permission
from inhere_box.infra.desktop.setup import uninstall_desktop_stream
from inhere_box.infra.users.identity import User
from inhere_box.infra.utils.locale import resolve_request_locale

router = APIRouter()


@router.post("/desktop/uninstall")
async def uninstall_desktop(
    request: Request,
    _user: User = Depends(require_permission("desktop")),
) -> StreamingResponse:
    """Stream virtual desktop uninstall progress as SSE (admin only)."""
    locale = resolve_request_locale(request)

    async def _event_stream() -> AsyncGenerator[str, None]:
        async for event in uninstall_desktop_stream(locale=locale):
            yield event

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
