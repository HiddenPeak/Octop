"""Request-local box authorization carried into domain admission and client tools."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop.infra.auth.box_accounts import BoxAccountService, BoxAuthContext


box_request_auth: ContextVar[tuple[BoxAccountService, BoxAuthContext] | None] = ContextVar(
    "box_request_auth", default=None
)


def validate_request(owner_id: int, *, conn: Any = None, client_id: str | None = None) -> None:
    binding = box_request_auth.get()
    if binding is None:
        raise OctopError.localized(ErrorCode.AUTH_FAILED)
    service, context = binding
    service.validate_context(context, conn=conn)
    if context.user.id != owner_id or (
        client_id is not None and client_id != context.paired_client_id
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_client_mismatch"})


def validate_client_tools(config: Any, owner_id: int, client_id: str, conn: Any) -> None:
    if getattr(config, "account_mode", "server") == "box_local":
        validate_request(owner_id, conn=conn, client_id=client_id)
