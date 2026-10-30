"""Account lifecycle boundaries for server and single-owner box installations."""

from __future__ import annotations

from octop.config import OctopConfig
from octop.infra.errors import ErrorCode, OctopError


def require_server_account_mode(config: OctopConfig) -> None:
    """Reject legacy account management and login in pairing-only box mode."""
    if getattr(config, "account_mode", "server") == "box_local":
        raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_account_mode"})
