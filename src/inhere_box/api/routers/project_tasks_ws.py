"""Task and resident conversations over one authenticated WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from inhere_box.api.common.box_auth import require_box_command
from inhere_box.api.common.task_websocket_logging import Scope, TaskWebSocketLog
from inhere_box.api.common.websocket_auth import has_box_auth, resolve_websocket_user
from inhere_box.api.deps import (
    extract_raw_token,
    maybe_sliding_renew_token,
    resolve_user_from_token,
)
from inhere_box.api.routers.projects import _services, _task_worker
from inhere_box.api.routers.resident_conversations import _runtime as _resident_runtime
from inhere_box.infra.agents.resident_history import timeline_sync
from inhere_box.infra.conversations.updates import PendingUpdates
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects import (
    approval_policy,
    client_reviews,
    client_tools,
    interactions,
    task_submission,
    tasks,
    title,
)
from inhere_box.infra.projects.approval_models import ApprovalPolicyUpdate
from inhere_box.infra.projects.client_tool_models import (
    ClaimToken,
    ClientID,
    ClientToolCall,
    ClientToolClaim,
    ClientToolResult,
    ClientToolReviewRequest,
)
from inhere_box.infra.projects.cursors import cursor_codec
from inhere_box.infra.projects.interaction_models import InteractionResponse, TaskControl
from inhere_box.infra.projects.models import ProjectModel, Reference
from inhere_box.infra.utils.locale import resolve_locale, resolve_request_locale

router = APIRouter(prefix="/project-tasks")
_AUTH_CHECK_SECONDS = 10.0
_UPDATE_SECONDS = 0.5
_TOOL_RECHECK_SECONDS = 5.0
_MAX_SUBSCRIPTIONS = 32
_MAX_FRAME_BYTES = 512 * 1024
EventCursor = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
OpenID = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ),
]


class AuthenticateFrame(ProjectModel):
    type: Literal["authenticate"]
    token: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    locale: Literal["zh", "en"] | None = None


class CommandFrame(ProjectModel):
    id: tasks.RequestID = Field(description="关联本次命令响应；网络重发使用新的命令 ID。")


class CreateFrame(CommandFrame):
    type: Literal["task.create"]
    data: tasks.TaskCreate


class MessageFrame(CommandFrame):
    type: Literal["task.message"]
    task_id: Reference
    data: tasks.TaskMessage


class RetryFrame(CommandFrame):
    type: Literal["task.retry"]
    task_id: Reference
    turn_id: Reference
    data: tasks.TaskRetry


class CancelFrame(CommandFrame):
    type: Literal["task.cancel"]
    task_id: Reference
    data: tasks.TaskCancel


class PauseFrame(CommandFrame):
    type: Literal["task.pause", "task.resume"]
    task_id: Reference
    data: TaskControl


class ApprovalPolicyFrame(CommandFrame):
    type: Literal["task.approval.set"]
    task_id: Reference
    data: ApprovalPolicyUpdate


class InteractionFrame(CommandFrame):
    type: Literal["task.interaction.respond"]
    task_id: Reference
    turn_id: Reference
    interaction_id: Reference
    data: InteractionResponse


class OpenFrame(CommandFrame):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"resume": {"const": True}}, "required": ["resume"]},
            "then": {"not": {"required": ["open_id"]}},
            "else": {"required": ["open_id"]},
        }
    )

    type: Literal["task.open"]
    task_id: Reference = Field(description="要打开并观察正文的任务。")
    cursor: EventCursor | None = Field(
        default=None, description="实时续接游标。已有观察时，显式打开使用该观察已应用的游标。"
    )
    resume: StrictBool = Field(
        default=False, description="恢复观察时传 true。默认表示用户明确进入，并按需补生成标题。"
    )
    open_id: OpenID | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
        description="本次显式打开的 UUID。重试复用同一个值，恢复观察时省略。",
    )

    @model_validator(mode="after")
    def validate_open_id(self) -> OpenFrame:
        if self.resume:
            if "open_id" in self.model_fields_set:
                raise ValueError("resume must omit open_id")
        elif self.open_id is None:
            raise ValueError("explicit task.open requires open_id")
        else:
            self.open_id = self.open_id.lower()
        return self


class UnsubscribeFrame(CommandFrame):
    type: Literal["task.unsubscribe"]
    task_id: Reference


class ResidentSubscribeFrame(CommandFrame):
    type: Literal["resident.subscribe"]
    agent_id: Reference
    thread_id: Reference
    timeline: Literal[True] = Field(description="Subscribe to the resumable resident timeline.")
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


class ResidentUnsubscribeFrame(CommandFrame):
    type: Literal["resident.unsubscribe"]
    agent_id: Reference
    thread_id: Reference


class PingFrame(CommandFrame):
    type: Literal["ping"]


class ReplaceSessionFrame(CommandFrame):
    type: Literal["session.replace"]
    access_token: str = Field(min_length=1, max_length=8192, repr=False)


class AttachFrame(CommandFrame):
    type: Literal["client.attach"]
    client_id: ClientID = Field(description="稳定的客户端安装标识；连接内不可切换。")


class ToolIdentityFrame(CommandFrame):
    task_id: Reference
    turn_id: Reference
    call_id: Reference


class ToolGetFrame(ToolIdentityFrame):
    type: Literal["tool.get"]


class ToolClaimFrame(ToolIdentityFrame):
    type: Literal["tool.claim"]
    request_id: tasks.RequestID = Field(description="本次领取的稳定标识；恢复领取必须复用。")


class ToolStartFrame(ToolIdentityFrame):
    type: Literal["tool.start"]
    claim_token: ClaimToken


class ToolResultFrame(ToolIdentityFrame):
    type: Literal["tool.result"]
    data: ClientToolResult


class ToolReviewFrame(ToolIdentityFrame):
    type: Literal["tool.review"]
    data: ClientToolReviewRequest


ConversationCommand = Annotated[
    CreateFrame
    | MessageFrame
    | RetryFrame
    | CancelFrame
    | PauseFrame
    | ApprovalPolicyFrame
    | InteractionFrame
    | OpenFrame
    | UnsubscribeFrame
    | ResidentSubscribeFrame
    | ResidentUnsubscribeFrame
    | PingFrame
    | ReplaceSessionFrame
    | AttachFrame
    | ToolGetFrame
    | ToolClaimFrame
    | ToolStartFrame
    | ToolResultFrame
    | ToolReviewFrame,
    Field(discriminator="type"),
]
_command: TypeAdapter[ConversationCommand] = TypeAdapter(ConversationCommand)
_command_id: TypeAdapter[tasks.RequestID] = TypeAdapter(tasks.RequestID)


@router.websocket("/ws")
async def task_websocket(websocket: WebSocket) -> None:
    """Shared commands and conversation snapshots, independent of worker lifetime."""
    diagnostics = TaskWebSocketLog(websocket.headers)
    diagnostics.record("connection_received")
    try:
        await _task_websocket(websocket, diagnostics)
    except (WebSocketDisconnect, OSError) as exc:
        diagnostics.disconnected(exc, stage="connection" if diagnostics.accepted else "accept")
    except asyncio.CancelledError:
        if diagnostics.close_source == "unknown":
            diagnostics.close_source = "cancelled"
        raise
    except Exception as exc:
        diagnostics.record("unexpected_error", stage="connection", exception=exc)
        if diagnostics.accepted:
            try:
                diagnostics.record("close_requested", close_code=1011)
                await websocket.close(code=1011)
                diagnostics.record("close_sent", close_code=1011)
            except (WebSocketDisconnect, OSError) as close_exc:
                diagnostics.disconnected(close_exc, stage="close")
            except Exception as close_exc:
                diagnostics.record("unexpected_error", stage="close", exception=close_exc)
    finally:
        diagnostics.record("connection_closed")


async def _task_websocket(websocket: WebSocket, diagnostics: TaskWebSocketLog) -> None:
    server = websocket.app.state.inhere_box_server
    # A close before accept becomes HTTP 403, losing the service failure reason.
    await websocket.accept(headers=diagnostics.response_headers)
    diagnostics.accepted = True
    diagnostics.record("connection_accepted")
    locale = resolve_request_locale(websocket)
    closing = False
    # Commands, pushes and terminal errors share the same send ordering.
    lock = asyncio.Lock()

    async def error(
        exc: InhereBoxError,
        *,
        stage: str,
        scope: Scope = "connection",
        command_type: str | None = None,
        **identity: str,
    ) -> None:
        diagnostics.record(
            "error",
            stage=stage,
            scope=scope,
            status=exc.status,
            error_code=exc.code.value,
            command_type=command_type,
        )
        await websocket.send_json(
            {"type": "error", **identity, "status": exc.status, **exc.to_envelope(locale=locale)}
        )

    async def close_with_error(exc: InhereBoxError, *, stage: str) -> None:
        nonlocal closing
        try:
            async with lock:
                if closing:
                    return
                closing = True
                await error(exc, stage=stage)
                code = 1011
                if exc.status == 503:
                    code = 1013
                elif exc.status in {401, 403} and exc.code in {
                    ErrorCode.AUTH_FAILED,
                    ErrorCode.TOKEN_EXPIRED,
                    ErrorCode.USER_DISABLED,
                }:
                    code = 4401
                diagnostics.record("close_requested", stage=stage, close_code=code)
                await websocket.close(code=code, reason=exc.code.value)
                diagnostics.record("close_sent", stage=stage, close_code=code)
        except (WebSocketDisconnect, OSError) as close_exc:
            diagnostics.disconnected(close_exc, stage="close")

    async def unexpected_error(exc: Exception, *, stage: str) -> None:
        diagnostics.record("unexpected_error", stage=stage, exception=exc)
        try:
            await close_with_error(InhereBoxError.localized(ErrorCode.INTERNAL_ERROR), stage=stage)
        except (WebSocketDisconnect, OSError) as close_exc:
            diagnostics.disconnected(close_exc, stage="close")
        except Exception as close_exc:
            diagnostics.record("unexpected_error", stage="close", exception=close_exc)

    try:
        services = _services(server)
    except InhereBoxError as exc:
        await close_with_error(exc, stage="service_check")
        return
    token = extract_raw_token(authorization=websocket.headers.get("authorization"))
    subscriptions: dict[str, str] = {}
    update_watches: dict[str, Callable[[], None]] = {}
    resident_subscriptions: dict[tuple[str, str], str] = {}
    resident_watches: dict[tuple[str, str], Callable[[], None]] = {}
    pending_updates = PendingUpdates[tuple[str, ...]]()
    client_id: str | None = None
    delivered_calls: dict[str, ClientToolCall] = {}
    stop_tool_watch: Callable[[], None] | None = None
    tool_update_key = ("client_tools",)
    pump: asyncio.Task[None] | None = None

    def queue_update(task_id: str) -> None:
        pending_updates.add(("task", task_id))

    def stop_watching(task_id: str) -> None:
        stop = update_watches.pop(task_id, None)
        if stop is not None:
            stop()
        pending_updates.discard(("task", task_id))

    def require_subscription_slot(task_id: str) -> None:
        if (
            task_id not in subscriptions
            and len(subscriptions) + len(resident_subscriptions) >= _MAX_SUBSCRIPTIONS
        ):
            raise InhereBoxError.localized(ErrorCode.TASK_FRAME_INVALID)

    def watched_cursor_covered(task_id: str, supplied_cursor: str, active_cursor: str) -> bool:
        """Whether the active watch already reaches a cursor the client has applied."""
        codec = cursor_codec(services, user.id, f"conversation:{task_id}:events")
        supplied = codec.decode(supplied_cursor, "task_events")
        if supplied_cursor == active_cursor:
            return True
        active = codec.decode(active_cursor, "task_events")
        # Zero history_position means that revision is fully applied.
        supplied_history = (
            supplied.history_revision,
            supplied.history_position == 0,
            supplied.history_position,
        )
        active_history = (
            active.history_revision,
            active.history_position == 0,
            active.history_position,
        )
        # A cursor from a newer REST read can lead the pushed watch; catch up from it.
        return (
            supplied.position <= active.position
            and supplied.summary_revision <= active.summary_revision
            and supplied_history <= active_history
        )

    async def send_task_updates(task_id: str, cursor: str, *, catch_up: bool = False) -> str:
        watermark = None
        while True:
            batch = await asyncio.to_thread(
                tasks.stream_updates, services, user.id, task_id, cursor, watermark=watermark
            )
            for update in batch.updates:
                if isinstance(update, tasks.TaskSummaryUpdate):
                    kind, data = "task.summary", update.model_dump(mode="json")
                else:
                    kind = "turn"
                    data = update.model_dump(
                        mode="json", exclude={"historical"} if update.historical is None else None
                    )
                await websocket.send_json({"type": kind, "data": data})
            cursor = batch.cursor
            if not batch.has_more:
                return cursor
            if not catch_up:
                queue_update(task_id)
                return cursor
            watermark = batch.watermark

    async def subscribe_task(task_id: str, cursor: str | None) -> None:
        if cursor is None:
            snapshot = await asyncio.to_thread(tasks.detail, services, user.id, task_id)
            await websocket.send_json(
                {"type": "snapshot", "data": snapshot.model_dump(mode="json")}
            )
            cursor = snapshot.event_cursor
        assert cursor is not None
        cursor = await send_task_updates(task_id, cursor, catch_up=True)
        subscriptions[task_id] = cursor
        stop_watching(task_id)
        worker = server.project_task_worker
        if worker is not None:
            update_watches[task_id] = worker.watch_updates(task_id, partial(queue_update, task_id))
        # Cover commits after the catch-up watermark or before registering the watch.
        queue_update(task_id)

    def queue_resident_update(scope: tuple[str, str]) -> None:
        pending_updates.add(("resident", *scope))

    def stop_resident_watch(scope: tuple[str, str]) -> None:
        stop = resident_watches.pop(scope, None)
        if stop is not None:
            stop()
        pending_updates.discard(("resident", *scope))

    async def send_resident_timeline(scope: tuple[str, str], cursor: str | None) -> str:
        agent_id, thread_id = scope
        update = await asyncio.to_thread(
            timeline_sync, services, user.id, agent_id, thread_id, cursor, locale
        )
        if update is None:
            assert cursor is not None
            return cursor
        await websocket.send_json(
            {
                "type": "resident.timeline.snapshot"
                if cursor is None
                else "resident.timeline.update",
                "data": update.model_dump(mode="json"),
            }
        )
        return update.cursor

    async def authenticate(stage: str) -> None:
        nonlocal token
        diagnostics.auth_check_count += 1
        try:
            if has_box_auth(websocket):
                current = await asyncio.to_thread(resolve_websocket_user, websocket, token)
            else:
                assert token is not None
                current = await asyncio.to_thread(resolve_user_from_token, server, token)
            if current.id != user.id:
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED)
            if has_box_auth(websocket):
                return
            assert token is not None
            renewed = await asyncio.to_thread(maybe_sliding_renew_token, server, token, current)
        except InhereBoxError as exc:
            diagnostics.record(
                "authentication_failed", stage=stage, status=exc.status, error_code=exc.code.value
            )
            raise
        if renewed:
            token = renewed
            await websocket.send_json({"type": "session", "access_token": renewed})
            diagnostics.record("session_renewed", stage=stage)

    async def push_updates() -> None:
        checked = time.monotonic()
        next_poll = checked + _UPDATE_SECONDS
        next_tools = checked + _TOOL_RECHECK_SECONDS
        delivered_policy = None
        stage = "idle"
        try:
            while True:
                next_wakeup = min(next_poll, next_tools) if client_id is not None else next_poll
                await pending_updates.wait(max(0, next_wakeup - time.monotonic()))
                # Clear before reading: commits during a read must wake the next pass.
                changed = pending_updates.take()
                poll = time.monotonic() >= next_poll
                async with lock:
                    if closing:
                        return
                    if time.monotonic() - checked >= _AUTH_CHECK_SECONDS:
                        stage = "authentication_idle"
                        await authenticate(stage)
                        await websocket.send_json({"type": "heartbeat"})
                        checked = time.monotonic()
                    if poll:
                        stage = "approval_policy"
                        policy = await asyncio.to_thread(approval_policy.current, services, user.id)
                        if policy != delivered_policy:
                            await websocket.send_json(
                                {"type": "approval.policy", "data": policy.model_dump(mode="json")}
                            )
                            delivered_policy = policy
                    for task_id, cursor in list(subscriptions.items()):
                        if not poll and ("task", task_id) not in changed:
                            continue
                        stage = "subscription_updates"
                        try:
                            cursor = await send_task_updates(task_id, cursor)
                        except InhereBoxError as exc:
                            subscriptions.pop(task_id, None)
                            stop_watching(task_id)
                            await error(exc, stage=stage, scope="subscription", task_id=task_id)
                            continue
                        subscriptions[task_id] = cursor
                    for scope, cursor in list(resident_subscriptions.items()):
                        if not poll and ("resident", *scope) not in changed:
                            continue
                        stage = "resident_subscription_updates"
                        try:
                            resident_subscriptions[scope] = await send_resident_timeline(
                                scope, cursor
                            )
                        except InhereBoxError as exc:
                            resident_subscriptions.pop(scope, None)
                            stop_resident_watch(scope)
                            await error(
                                exc,
                                stage=stage,
                                scope="subscription",
                                agent_id=scope[0],
                                thread_id=scope[1],
                            )
                    if client_id is not None and (
                        tool_update_key in changed or time.monotonic() >= next_tools
                    ):
                        stage = "client_tools"
                        calls = await asyncio.to_thread(
                            client_tools.open_calls,
                            services,
                            user.id,
                            client_id,
                            tuple(delivered_calls),
                        )
                        current_ids = {call.call_id for call in calls}
                        for call_id, previous in delivered_calls.items():
                            if call_id not in current_ids:
                                calls.append(
                                    previous.model_copy(
                                        update={"status": "interrupted", "dispatch_allowed": False}
                                    )
                                )
                        next_tools = time.monotonic() + _TOOL_RECHECK_SECONDS
                        for call in calls:
                            if delivered_calls.get(call.call_id) == call:
                                continue
                            active = call.status in {"pending", "claimed"}
                            await websocket.send_json(
                                {
                                    "type": "tool.call" if active else "tool.closed",
                                    "data": call.model_dump(mode="json"),
                                }
                            )
                            if active:
                                delivered_calls[call.call_id] = call
                            else:
                                delivered_calls.pop(call.call_id, None)
                    if poll:
                        next_poll = time.monotonic() + _UPDATE_SECONDS
        except InhereBoxError as exc:
            await close_with_error(exc, stage=stage)
        except (WebSocketDisconnect, OSError) as exc:
            diagnostics.disconnected(exc, stage=stage)
        except Exception as exc:
            await unexpected_error(exc, stage=stage)

    stage = "authentication_initial"
    diagnostics.auth_check_count += 1
    diagnostics.record("authentication_started", stage=stage)
    try:
        if token is None and not has_box_auth(websocket):
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
                auth = AuthenticateFrame.model_validate_json(raw)
                token = auth.token
                locale = auth.locale or locale
            except (TimeoutError, ValidationError):
                raise InhereBoxError.localized(ErrorCode.AUTH_FAILED) from None
        if has_box_auth(websocket):
            user = await asyncio.to_thread(resolve_websocket_user, websocket, token)
        else:
            assert token is not None
            user = await asyncio.to_thread(resolve_user_from_token, server, token)
        locale = resolve_locale(user_locale=user.locale, explicit=locale)
        diagnostics.record("authentication_succeeded", stage=stage)
        stage = "ready"
        replace_session = getattr(websocket.state, "replace_box_session", None)
        ready = {"type": "ready", "protocol_version": 3}
        if replace_session is not None:
            ready["session_replace"] = True
        await websocket.send_json(ready)
        diagnostics.record("ready")
        pump = asyncio.create_task(push_updates())
        while True:
            stage = "receive"
            raw = await websocket.receive_text()
            async with lock:
                if closing:
                    return
                if len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
                    closing = True
                    diagnostics.record("close_requested", stage="frame_size", close_code=1009)
                    await websocket.close(code=1009)
                    diagnostics.record("close_sent", stage="frame_size", close_code=1009)
                    return
                stage = "authentication_command"
                await authenticate(stage)
                stage = "command"
                try:
                    frame = _command.validate_json(raw)
                except ValidationError:
                    # Never echo invalid inputs: they may contain prompts or tokens.
                    identity = {}
                    try:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            identity["id"] = _command_id.validate_python(payload.get("id"))
                    except (ValueError, ValidationError):
                        pass
                    await error(
                        InhereBoxError.localized(ErrorCode.TASK_FRAME_INVALID),
                        stage=stage,
                        scope="command",
                        **identity,
                    )
                    continue
                diagnostics.command_count += 1
                box_context = getattr(websocket.state, "box_auth_context", None)
                try:
                    if box_context is not None:
                        require_box_command(box_context.role, frame.type)
                except InhereBoxError as exc:
                    await error(
                        exc, stage=stage, scope="command", command_type=frame.type, id=frame.id
                    )
                    continue
            if isinstance(frame, (CreateFrame, MessageFrame, RetryFrame)):
                # Admission can await hybrid model classification. Keep the
                # receive order, but let the push pump use the connection.
                try:
                    worker = _task_worker(server)
                    detail = await task_submission.submit(
                        services,
                        user.id,
                        frame.data,
                        project_id=frame.data.project_id
                        if isinstance(frame, CreateFrame)
                        else None,
                        task_id=None if isinstance(frame, CreateFrame) else frame.task_id,
                        retry_of_turn_id=frame.turn_id if isinstance(frame, RetryFrame) else None,
                    )
                    worker.wake()
                except InhereBoxError as exc:
                    async with lock:
                        if not closing:
                            await error(
                                exc,
                                stage=stage,
                                scope="command",
                                command_type=frame.type,
                                id=frame.id,
                            )
                else:
                    async with lock:
                        if not closing:
                            await websocket.send_json(
                                {
                                    "type": "result",
                                    "id": frame.id,
                                    "data": detail.model_dump(mode="json"),
                                }
                            )
                continue
            async with lock:
                if closing:
                    return
                try:
                    result: Any = None
                    if isinstance(frame, ReplaceSessionFrame):
                        if replace_session is None:
                            raise InhereBoxError.localized(ErrorCode.FORBIDDEN)
                        await replace_session(frame.access_token)
                    elif isinstance(frame, AttachFrame):
                        if getattr(server, "box_account_service", None) is not None:
                            from inhere_box.infra.auth.box_context import validate_request

                            await asyncio.to_thread(
                                validate_request, user.id, client_id=frame.client_id
                            )
                        if client_id is not None and frame.client_id != client_id:
                            raise InhereBoxError.localized(ErrorCode.TASK_FRAME_INVALID)
                        client_id = frame.client_id
                        if stop_tool_watch is None:
                            stop_tool_watch = services.client_tool_updates.watch(
                                (user.id, client_id), lambda: pending_updates.add(tool_update_key)
                            )
                            pending_updates.add(tool_update_key)
                    elif isinstance(frame, ToolIdentityFrame):
                        if client_id is None:
                            raise InhereBoxError.localized(ErrorCode.TASK_FRAME_INVALID)
                        args = (
                            services,
                            user.id,
                            client_id,
                            frame.task_id,
                            frame.turn_id,
                            frame.call_id,
                        )
                        receipt: ClientToolCall | ClientToolClaim
                        if isinstance(frame, ToolReviewFrame):
                            receipt = await asyncio.to_thread(
                                client_reviews.request_review, *args, frame.data
                            )
                            if server.project_task_worker is not None:
                                server.project_task_worker.review_worker.wake()
                        elif isinstance(frame, ToolClaimFrame):
                            receipt = await asyncio.to_thread(
                                client_tools.claim_call, *args, frame.request_id
                            )
                        elif isinstance(frame, ToolStartFrame):
                            receipt = await asyncio.to_thread(
                                client_tools.start_execution, *args, frame.claim_token
                            )
                        elif isinstance(frame, ToolResultFrame):
                            receipt = await asyncio.to_thread(
                                client_tools.submit_result, *args, frame.data
                            )
                            if server.project_task_worker is not None:
                                server.project_task_worker.wake()
                        else:
                            receipt = await asyncio.to_thread(client_tools.get_call, *args)
                        if not isinstance(frame, ToolGetFrame):
                            services.client_tool_updates.notify((user.id, client_id))
                        result = receipt.model_dump(mode="json")
                    elif isinstance(frame, OpenFrame):
                        require_subscription_slot(frame.task_id)
                        active_cursor = (
                            subscriptions.get(frame.task_id) if not frame.resume else None
                        )
                        covered = (
                            frame.cursor is not None
                            and active_cursor is not None
                            and await asyncio.to_thread(
                                watched_cursor_covered, frame.task_id, frame.cursor, active_cursor
                            )
                        )
                        if not frame.resume:
                            assert frame.open_id is not None
                            worker = _task_worker(server)
                            admitted = await asyncio.to_thread(
                                title.admit_open_title,
                                services,
                                user.id,
                                frame.task_id,
                                frame.open_id,
                            )
                            if admitted:
                                worker.title_worker.wake()
                        if not covered:
                            await subscribe_task(frame.task_id, frame.cursor)
                    elif isinstance(frame, UnsubscribeFrame):
                        subscriptions.pop(frame.task_id, None)
                        stop_watching(frame.task_id)
                    elif isinstance(frame, ResidentSubscribeFrame):
                        resident_scope = (frame.agent_id, frame.thread_id)
                        if (
                            resident_scope not in resident_subscriptions
                            and len(subscriptions) + len(resident_subscriptions)
                            >= _MAX_SUBSCRIPTIONS
                        ):
                            raise InhereBoxError.localized(ErrorCode.TASK_FRAME_INVALID)
                        resident = _resident_runtime(server)
                        cursor = frame.cursor
                        if cursor is None:
                            cursor = await send_resident_timeline(resident_scope, None)
                        resident_subscriptions[resident_scope] = await send_resident_timeline(
                            resident_scope, cursor
                        )
                        stop_resident_watch(resident_scope)
                        resident_watches[resident_scope] = resident.watch_updates(
                            frame.thread_id, partial(queue_resident_update, resident_scope)
                        )
                        queue_resident_update(resident_scope)
                    elif isinstance(frame, ResidentUnsubscribeFrame):
                        resident_scope = (frame.agent_id, frame.thread_id)
                        resident_subscriptions.pop(resident_scope, None)
                        stop_resident_watch(resident_scope)
                    elif isinstance(frame, ApprovalPolicyFrame):
                        detail = await asyncio.to_thread(
                            approval_policy.set_policy,
                            services,
                            user.id,
                            frame.task_id,
                            frame.data,
                        )
                        result = detail.model_dump(mode="json")
                    elif isinstance(frame, (PauseFrame, InteractionFrame)):
                        if isinstance(frame, PauseFrame):
                            await asyncio.to_thread(
                                interactions.control,
                                services,
                                user.id,
                                frame.task_id,
                                frame.data,
                                paused=frame.type == "task.pause",
                            )
                        else:
                            await asyncio.to_thread(
                                interactions.respond,
                                services,
                                user.id,
                                frame.task_id,
                                frame.turn_id,
                                frame.interaction_id,
                                frame.data,
                            )
                        if server.project_task_worker is not None:
                            server.project_task_worker.wake()
                        detail = await asyncio.to_thread(
                            tasks.detail, services, user.id, frame.task_id
                        )
                        result = detail.model_dump(mode="json")
                    elif isinstance(frame, CancelFrame):
                        detail = await asyncio.to_thread(
                            tasks.cancel, services, user.id, frame.task_id, frame.data.turn_id
                        )
                        result = detail.model_dump(mode="json")
                    await websocket.send_json({"type": "result", "id": frame.id, "data": result})
                except InhereBoxError as exc:
                    await error(
                        exc, stage=stage, scope="command", command_type=frame.type, id=frame.id
                    )
    except InhereBoxError as exc:
        if stage == "authentication_initial":
            diagnostics.record(
                "authentication_failed", stage=stage, status=exc.status, error_code=exc.code.value
            )
        await close_with_error(exc, stage=stage)
    except (WebSocketDisconnect, OSError) as exc:
        diagnostics.disconnected(exc, stage=stage)
    except Exception as exc:
        await unexpected_error(exc, stage=stage)
    finally:
        diagnostics.subscription_count = len(subscriptions) + len(resident_subscriptions)
        for task_id in list(update_watches):
            stop_watching(task_id)
        for resident_scope in list(resident_watches):
            stop_resident_watch(resident_scope)
        if stop_tool_watch is not None:
            stop_tool_watch()
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
