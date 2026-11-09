"""Box mode gates every transport, including legacy long-lived connections."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import Any

from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket

from inhere_box.api.common.box_auth import (
    BOX_CSRF_HEADER,
    browser_cookie_name,
    require_box_route,
    require_browser_origin,
)
from inhere_box.infra.auth.box_context import BoxRequestAuthorization, box_request_auth
from inhere_box.infra.errors import ErrorCode, InhereBoxError

_RECHECK_SECONDS = 1.0
_PUBLIC = {
    ("GET", "/api/box/status"),
    ("POST", "/api/box/pairings"),
    ("POST", "/api/box/owner-claims"),
    ("POST", "/api/auth/challenges"),
    ("POST", "/api/auth/sessions"),
    ("POST", "/api/auth/client-logins"),
    ("POST", "/api/auth/browser/sessions"),
    ("GET", "/api/docs"),
    ("GET", "/api/openapi.json"),
}
_PAIRING_PUBLIC = re.compile(r"/api/box/pairings/[A-Za-z0-9_-]+/(?:proof|result)\Z")
_CLIENT_STATUS_PUBLIC = re.compile(r"/api/box/clients/[A-Za-z0-9_-]+/status\Z")
_BLOCKED = (
    "/api/setup",
    "/api/bridge",
    "/api/user-roles",
    "/api/auth/oidc",
    "/api/auth/oauth",
    "/api/auth/ldap",
    "/api/auth/invite",
    "/api/downloads/link",
)


def public_box_path(method: str, path: str) -> bool:
    return (
        (method, path) in _PUBLIC
        or (method == "POST" and _PAIRING_PUBLIC.fullmatch(path) is not None)
        or (method == "POST" and _CLIENT_STATUS_PUBLIC.fullmatch(path) is not None)
        or (method == "GET" and path.startswith("/api/i18n/"))
    )


def blocked_box_path(method: str, path: str) -> bool:
    if path in {"/api/auth/login", "/api/auth/captcha", "/api/auth/change-password"}:
        return True
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _BLOCKED):
        return True
    if path == "/api/users" or path.startswith("/api/users/"):
        return not (method == "GET" and re.fullmatch(r"/api/users/[0-9]+/avatar", path))
    return False


class BoxAccountMiddleware:
    def __init__(self, app: ASGIApp, *, server: Any) -> None:
        self.app, self.server = app, server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        config = getattr(self.server, "config", None)
        if config is None and getattr(self.server, "services", None) is not None:
            config = self.server.services.config
        if (
            scope["type"] not in {"http", "websocket"}
            or getattr(config, "account_mode", "server") != "box_local"
            or not scope.get("path", "").startswith("/api/")
        ):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        method = scope.get("method", "WEBSOCKET")
        try:
            if blocked_box_path(method, path):
                raise InhereBoxError.localized(
                    ErrorCode.FORBIDDEN, details={"reason": "box_account_mode"}
                )
            if method == "GET" and path == "/api/health":
                await JSONResponse({"ok": True})(scope, receive, send)
                return
            if public_box_path(method, path):
                await self.app(scope, receive, send)
                return
            service = getattr(self.server, "box_account_service", None)
            if service is None:
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
            # These adapters validate their own task/connector-scoped credentials.
            # They do not issue a box owner session.
            if path.startswith(("/api/internal/mcp/", "/api/connectors/oauth/callback")):
                if await asyncio.to_thread(service.owner_user) is None:
                    raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
                await self.app(scope, receive, send)
                return
            if any(
                key in QueryParams(scope.get("query_string", b""))
                for key in ("token", "access_token")
            ):
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
            headers = Headers(scope=scope)
            authorization = headers.get("authorization")
            using_cookie = authorization is None
            if using_cookie:
                raw = HTTPConnection(scope).cookies.get(browser_cookie_name(service.box_id), "")
                if not raw:
                    raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
                require_browser_origin(scope, safe=method in {"GET", "HEAD", "OPTIONS"})
            elif authorization is not None and authorization.lower().startswith("bearer "):
                raw = authorization[7:].strip()
            else:
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
            context = await asyncio.to_thread(service.validate_access_token, raw)
            if using_cookie != (context.session_kind == "admin"):
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
            require_box_route(context.role, method, path)
            if using_cookie and method not in {"GET", "HEAD", "OPTIONS", "WEBSOCKET"}:
                await asyncio.to_thread(
                    service.require_browser_csrf, context, headers.get(BOX_CSRF_HEADER, "")
                )
        except InhereBoxError as exc:
            await self._reject(scope, receive, send, exc)
            return
        state = scope.setdefault("state", {})
        state["box_auth_context"] = context
        state["box_auth_transport"] = "cookie" if using_cookie else "bearer"
        state["inhere_box_user"] = context.user
        authorization_context = BoxRequestAuthorization(service, context)
        binding = box_request_auth.set(authorization_context)
        try:
            await self._authenticated(scope, receive, send, authorization_context, raw)
        finally:
            box_request_auth.reset(binding)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, exc: InhereBoxError
    ) -> None:
        response = JSONResponse(
            exc.to_envelope(), status_code=exc.status, headers={"Cache-Control": "no-store"}
        )
        if scope["type"] == "websocket":
            if "websocket.http.response" in scope.get("extensions", {}):
                # A pre-accept close becomes an undifferentiated HTTP 403.
                # Preserve 401 so native clients can renew their existing device
                # session after a box restart, without treating scoped 403 as auth.
                await WebSocket(scope, receive, send).send_denial_response(response)
            else:
                # Older ASGI adapters cannot send an HTTP denial response.
                await send({"type": "websocket.close", "code": 4401})
        else:
            await response(scope, receive, send)

    async def _authenticated(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        authorization: BoxRequestAuthorization,
        token: str,
    ) -> None:
        service = authorization.service
        lost = asyncio.Event()
        failure: InhereBoxError | None = None
        started, complete = False, False
        send_lock = asyncio.Lock()

        auth_lock = asyncio.Lock()

        async def check() -> bool:
            nonlocal failure
            async with auth_lock:
                if lost.is_set():
                    return False
                try:
                    current = await asyncio.to_thread(service.validate_access_token, token)
                    require_box_route(current.role, scope.get("method", "WEBSOCKET"), scope["path"])
                    scope["state"]["box_auth_context"] = current
                    scope["state"]["inhere_box_user"] = current.user
                    return True
                except InhereBoxError as exc:
                    failure = exc
                    lost.set()
                    return False

        async def replace_token(replacement: str) -> None:
            nonlocal token
            async with auth_lock:
                if lost.is_set():
                    raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
                previous = await asyncio.to_thread(service.validate_access_token, token)
                candidate = await asyncio.to_thread(service.validate_access_token, replacement)
                if (
                    candidate.session_kind != "client"
                    or candidate.user.id != previous.user.id
                    or candidate.paired_client_id != previous.paired_client_id
                    or candidate.role != previous.role
                ):
                    raise InhereBoxError.localized(ErrorCode.FORBIDDEN)
                require_box_route(candidate.role, "WEBSOCKET", scope["path"])
                token = replacement
                authorization.context = candidate
                scope["state"]["box_auth_context"] = candidate
                scope["state"]["inhere_box_user"] = candidate.user

        if (
            scope["type"] == "websocket"
            and scope["path"] == "/api/project-tasks/ws"
            and authorization.context.session_kind == "client"
        ):
            scope["state"]["replace_box_session"] = replace_token

        async def guarded_receive() -> Message:
            message = await receive()
            if message["type"] == "websocket.receive" and not await check():
                # Wait for the monitor to close and cancel the handler. Never
                # deliver an unauthorized command to a legacy WS adapter.
                await asyncio.Future[None]()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started, complete
            if (
                message["type"] == "http.response.start"
                and scope["state"].get("box_auth_transport") == "cookie"
            ):
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            if message["type"] == "websocket.send" and not await check():
                await asyncio.Future[None]()
            async with send_lock:
                if complete:
                    return
                await send(message)
                if message["type"] in {"http.response.start", "websocket.accept"}:
                    started = True
                if message["type"] == "websocket.close" or (
                    message["type"] == "http.response.body" and not message.get("more_body", False)
                ):
                    complete = True

        async def monitor() -> None:
            while not lost.is_set():
                try:
                    await asyncio.wait_for(lost.wait(), timeout=_RECHECK_SECONDS)
                except TimeoutError:
                    await check()

        async def run_handler() -> None:
            await self.app(scope, guarded_receive, guarded_send)

        handler = asyncio.create_task(run_handler())
        watcher = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({handler, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if handler in done:
                await handler
                return
            async with send_lock:
                if not complete:
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 4401})
                    elif started:
                        await send({"type": "http.response.body", "body": b""})
                    else:
                        assert failure is not None
                        await self._reject(scope, receive, send, failure)
                    complete = True
        finally:
            for task in (watcher, handler):
                if not task.done():
                    task.cancel()
            for task in (watcher, handler):
                with suppress(asyncio.CancelledError):
                    await task
