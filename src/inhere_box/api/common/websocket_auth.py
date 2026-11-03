"""Shared identity resolution for legacy and box WebSocket adapters."""

from __future__ import annotations

from typing import cast

from fastapi import WebSocket

from inhere_box.api.common.box_auth import require_box_route
from inhere_box.api.deps import resolve_user_from_token
from inhere_box.infra.auth.box_accounts import BoxAuthContext
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.users.identity import User


def has_box_auth(websocket: WebSocket) -> bool:
    """Whether outer box middleware authenticated this connection's header."""
    return isinstance(
        getattr(getattr(websocket, "state", None), "box_auth_context", None), BoxAuthContext
    )


def resolve_websocket_user(websocket: WebSocket, legacy_token: str | None) -> User:
    """Resolve identity off the event loop, preserving server-mode query auth.

    Box sockets use only the context installed by the outer header-auth gate.
    Tokens supplied in an old start frame cannot replace that client identity.
    Rechecking returns the current owner permissions for existing scope guards;
    the outer middleware also closes idle or continuously streaming sessions.
    """
    server = websocket.app.state.inhere_box_server
    context = getattr(getattr(websocket, "state", None), "box_auth_context", None)
    box = getattr(server, "box_account_service", None)
    config = getattr(server, "config", None)
    if config is None:
        config = getattr(getattr(server, "services", None), "config", None)
    if box is not None or getattr(config, "account_mode", "server") == "box_local":
        if box is None or not isinstance(context, BoxAuthContext):
            raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
        checked = box.validate_context(context)
        require_box_route(checked.role, "WEBSOCKET", websocket.url.path)
        return cast("User", checked.user)
    if not legacy_token:
        raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
    return resolve_user_from_token(server, legacy_token)
