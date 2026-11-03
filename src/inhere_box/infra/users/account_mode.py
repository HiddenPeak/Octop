"""Account lifecycle boundaries for server and single-owner box installations."""

from __future__ import annotations

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.errors import ErrorCode, InhereBoxError


def require_server_account_mode(config: InhereBoxConfig) -> None:
    """Reject legacy account management and login in pairing-only box mode."""
    if getattr(config, "account_mode", "server") == "box_local":
        raise InhereBoxError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_account_mode"})
