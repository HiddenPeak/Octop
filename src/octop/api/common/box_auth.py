"""Transport requirements for local box authentication and business access."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers

from octop.infra.errors import ErrorCode, OctopError

BOX_CSRF_HEADER = "X-Octop-Box-CSRF"


def browser_cookie_name(box_id: str) -> str:
    # Cookies do not isolate ports; isolate boxes sharing a development host.
    return "octop_box_session_" + hashlib.sha256(box_id.encode()).hexdigest()[:16]


def _origin(value: str, *, referer: bool = False) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not referer and (parsed.path or parsed.query or parsed.fragment))
    ):
        raise ValueError("invalid origin")
    return (
        parsed.scheme,
        parsed.hostname.lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def require_browser_origin(scope: Mapping[str, Any], *, safe: bool = False) -> None:
    """Cookie credentials are restricted to this exact web origin.

    Safe fetches normally omit Origin. Fetch Metadata (or a same-origin Referer
    on older browsers) provides the same restriction in that case. Mutations
    and WebSockets always require an explicit Origin header.
    """
    headers = Headers(raw=scope.get("headers", []))
    scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
    try:
        expected = _origin(f"{scheme}://{headers.get('host', '')}")
        if scheme == "http":
            # Do not grant the cleartext development exception to DNS rebinding
            # hostnames, even when the connection reaches the loopback socket.
            hostname = expected[1]
            if hostname != "localhost" and not ip_address(hostname).is_loopback:
                raise ValueError("not localhost")
        origin = headers.get("origin")
        if origin is not None:
            allowed = len(headers.getlist("origin")) == 1 and _origin(origin) == expected
        elif safe and headers.get("sec-fetch-site") == "same-origin":
            allowed = True
        elif safe and headers.get("referer"):
            allowed = _origin(headers["referer"], referer=True) == expected
        else:
            allowed = False
        if allowed:
            return
    except ValueError:
        pass
    raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_origin_required"})


def require_box_transport(scope: Mapping[str, Any]) -> None:
    """Require TLS, allowing cleartext only from a loopback socket for development.

    The ASGI server supplies the scheme and peer. Forwarded client headers and
    a caller-controlled Host name cannot grant the loopback exception.
    """
    if scope.get("scheme") in {"https", "wss"}:
        return
    client = scope.get("client")
    try:
        if client and ip_address(str(client[0])).is_loopback:
            return
    except ValueError:
        pass
    raise OctopError.localized(
        ErrorCode.FORBIDDEN,
        details={"reason": "box_tls_required"},
    )


# Guest access is deliberately enumerated. New HTTP surfaces require an explicit
# decision here instead of inheriting owner access from a shared router prefix.
_GUEST_EXACT = {
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/box/account"),
    ("GET", "/api/settings/timezone"),
    ("GET", "/api/settings/upload"),
    ("GET", "/api/settings/hitl"),
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("GET", "/api/projects/sync"),
    ("GET", "/api/projects/resources"),
    ("GET", "/api/project-tasks/resources"),
    ("GET", "/api/project-tasks"),
    ("POST", "/api/project-tasks"),
    ("GET", "/api/project-tasks/sync"),
    ("GET", "/api/project-tasks/capabilities"),
    ("POST", "/api/project-tasks/attachments"),
    ("WEBSOCKET", "/api/project-tasks/ws"),
}
_GUEST_PATTERNS = (
    (frozenset({"GET", "PATCH", "PUT", "DELETE"}), r"/api/projects/[A-Za-z0-9_-]+"),
    (frozenset({"PATCH"}), r"/api/projects/[A-Za-z0-9_-]+/archive"),
    (frozenset({"GET", "POST"}), r"/api/projects/[A-Za-z0-9_-]+/tasks"),
    (frozenset({"GET"}), r"/api/projects/[A-Za-z0-9_-]+/tasks/sync"),
    (frozenset({"GET", "PATCH", "DELETE"}), r"/api/project-tasks/[A-Za-z0-9_-]+"),
    (frozenset({"GET"}), r"/api/project-tasks/[A-Za-z0-9_-]+/(?:context-usage|events)"),
    (frozenset({"GET"}), r"/api/project-tasks/attachments/[A-Za-z0-9_-]+"),
    (frozenset({"GET"}), r"/api/project-tasks/[A-Za-z0-9_-]+/artifacts/[A-Za-z0-9_-]+/download"),
    (
        frozenset({"POST"}),
        r"/api/project-tasks/[A-Za-z0-9_-]+/(?:messages|cancel|pause|resume|forks)",
    ),
    (frozenset({"POST"}), r"/api/project-tasks/[A-Za-z0-9_-]+/turns/[A-Za-z0-9_-]+/retry"),
    (
        frozenset({"POST"}),
        r"/api/project-tasks/[A-Za-z0-9_-]+/turns/[A-Za-z0-9_-]+/interactions/[A-Za-z0-9_-]+/respond",
    ),
)
# Static resource names must never be mistaken for resource IDs by the patterns.
_GUEST_RESERVED = {
    "/api/project-tasks/settings",
    "/api/project-tasks/engines",
    "/api/project-tasks/artifacts",
    "/api/project-tasks/approval-policy",
    "/api/project-tasks/from-thread",
}
_GUEST_COMMANDS = frozenset(
    {
        "ping",
        "client.attach",
        "tool.get",
        "tool.claim",
        "tool.result",
        "tool.review",
        "task.create",
        "task.message",
        "task.retry",
        "task.cancel",
        "task.pause",
        "task.resume",
        "task.interaction.respond",
        "task.open",
        "task.subscribe",
        "task.unsubscribe",
    }
)


def require_box_route(role: str, method: str, path: str) -> None:
    if role != "guest":
        return
    if path not in _GUEST_RESERVED and (
        (method, path) in _GUEST_EXACT
        or any(
            method in methods and re.fullmatch(pattern, path)
            for methods, pattern in _GUEST_PATTERNS
        )
    ):
        return
    raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_guest_forbidden"})


def require_box_command(role: str, command: str) -> None:
    if role == "guest" and command not in _GUEST_COMMANDS:
        raise OctopError.localized(ErrorCode.FORBIDDEN, details={"reason": "box_guest_forbidden"})
