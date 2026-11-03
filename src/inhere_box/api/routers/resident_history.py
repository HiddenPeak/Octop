"""Typed private resident history endpoints; no model execution or reset."""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response

from inhere_box.api.deps import current_user, get_server
from inhere_box.infra.agents.resident_history import (
    ResidentHistoryDays,
    ResidentHistoryLocation,
    ResidentHistoryPage,
    history_days,
    history_location,
    history_page,
)
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.server import InhereBoxServer
from inhere_box.infra.users.identity import User
from inhere_box.infra.utils.locale import resolve_request_locale

router = APIRouter()
_THREAD = "/agents/{agent_id}/threads/{thread_id}"


def _services(server: InhereBoxServer) -> SharedServices:
    if server.services is None:
        raise InhereBoxError.localized(ErrorCode.SETUP_REQUIRED)
    return server.services


@router.get(
    _THREAD + "/conversation/history/days",
    response_model=ResidentHistoryDays,
    summary="List the most recent nonempty resident conversation days",
    description="Defaults to seven nonempty server days. The signed cursor is private to this user and thread; at locates a date or its nearest earlier nonempty day.",
)
async def days(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(7, ge=1, le=31),
    cursor: str | None = Query(None, max_length=4096),
    at: date | None = None,
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentHistoryDays:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        history_days,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        cursor,
        at.isoformat() if at else "",
    )


@router.get(
    _THREAD + "/conversation/history/search",
    response_model=ResidentHistoryPage,
    summary="Search resident turn text and task reports without loading the full timeline",
    description="Literal substring search. Each bounded page is chronological; next_cursor continues toward older matches and is bound to the exact query.",
)
async def search(
    agent_id: str,
    thread_id: str,
    request: Request,
    response: Response,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(40, ge=1, le=100),
    cursor: str | None = Query(None, max_length=4096),
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentHistoryPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        history_page,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        cursor,
        user.locale or resolve_request_locale(request),
        query=q,
    )


@router.get(
    _THREAD + "/conversation/history/items/{item_id}",
    response_model=ResidentHistoryLocation,
    summary="Locate one stable resident history item with bounded surrounding context",
)
async def locate(
    agent_id: str,
    thread_id: str,
    item_id: str,
    request: Request,
    response: Response,
    before: int = Query(20, ge=0, le=50),
    after: int = Query(20, ge=0, le=50),
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentHistoryLocation:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        history_location,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        item_id,
        before,
        after,
        user.locale or resolve_request_locale(request),
    )


@router.get(
    _THREAD + "/conversation/history",
    response_model=ResidentHistoryPage,
    summary="Read a bounded resident timeline page, optionally for one frozen server day",
    description="Returns turns and durable task reports in one chronological page. next_cursor continues toward older items; it is not an array offset.",
)
async def page(
    agent_id: str,
    thread_id: str,
    request: Request,
    response: Response,
    day_key: date | None = None,
    limit: int = Query(80, ge=1, le=100),
    cursor: str | None = Query(None, max_length=4096),
    user: User = Depends(current_user),
    server: InhereBoxServer = Depends(get_server),
) -> ResidentHistoryPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        history_page,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        cursor,
        user.locale or resolve_request_locale(request),
        day_key=day_key.isoformat() if day_key else "",
    )
