"""Chart contracts: scope, calendar boundaries, unknowns and cross-day deduplication."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.integration.test_task_usage_summary import (
    _WHEN,
    _call,
    _historical_provider,
    _summary,
    _task,
    _tokens,
)
from tests.support.app import inhere_box_client, write_inhere_box_config
from tests.support.auth import create_user, resolve_user_id


async def _trend(
    client: Any, auth: dict[str, str], *, admin: bool = False, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        "/api/admin/usage/task-trend" if admin else "/api/usage/task-trend",
        headers=auth,
        params={"window": "month:2026-10", **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_trend_selects_once_per_task_then_buckets_and_matches_summary(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "Local", "box")
    _task(server, owner, "overlap", input_tokens=900_000, model_ref="Local/model")
    _task(server, owner, "history", input_tokens=100, output_tokens=10, model_ref="Local/model")
    _call(
        server,
        owner,
        "next-day",
        task_id="overlap",
        usage=_tokens(200, 20),
        started_at=_WHEN + 86_400_000,
    )
    _call(
        server, owner, "cloud", task_id="cloud-task", usage=_tokens(300, 30), source="third_party"
    )
    _call(server, owner, "unassigned", usage=_tokens(999, 999))

    trend = await _trend(client, auth)
    days = {day["date"]: day for day in trend["days"]}
    assert len(days) == 31 and list(days) == sorted(days)
    assert trend["excluded_unassigned_calls"] == 1
    assert days["2026-10-09"]["savings"]["status"] == "no_data"
    assert days["2026-10-09"]["cumulative_savings"]["status"] == "no_data"
    assert days["2026-10-10"]["sources"]["box"]["tokens"]["total_tokens"] == 110
    assert days["2026-10-10"]["sources"]["third_party"]["tokens"]["total_tokens"] == 330
    assert days["2026-10-10"]["savings"]["amount_usd"] == "0.0015"
    assert days["2026-10-11"]["savings"]["amount_usd"] == "0.003"
    assert days["2026-10-12"]["savings"]["status"] == "no_data"
    assert days["2026-10-12"]["cumulative_savings"]["amount_usd"] == "0.0045"
    assert trend["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 330
    assert trend["totals"]["sources"]["box"]["receipt_calls"] == 1
    assert trend["totals"]["sources"]["box"]["historical_records"] == 1
    assert trend["totals"]["savings"] == trend["days"][-1]["cumulative_savings"]
    summary = await _summary(client, auth, limit=1)
    assert trend["totals"]["savings"] == summary["totals"]["savings"]
    # Selecting a smaller window deliberately changes per-task fallback, like task-summary.
    day = await _trend(client, auth, window="day:2026-10-10", task_id="overlap")
    assert day["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 900_000


async def test_trend_unknown_zero_partial_and_range_survive_accumulation(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _historical_provider(server, "Local", "box")
    _task(
        server,
        owner,
        "long-history",
        input_tokens=300_000,
        output_tokens=20,
        model_ref="Local/model",
    )
    _call(
        server,
        owner,
        "zero",
        task_id="zero-task",
        source="third_party",
        started_at=_WHEN - 86_400_000,
    )
    _call(
        server,
        owner,
        "missing",
        task_id="missing-task",
        status="cancelled",
        started_at=_WHEN + 86_400_000,
    )
    _call(
        server,
        owner,
        "known",
        task_id="known-task",
        usage=_tokens(10, 1),
        started_at=_WHEN + 86_400_000,
    )
    _task(server, owner, "unknown-source", input_tokens=100, model_ref="missing/model")

    ranged = await _trend(client, auth, task_id="long-history")
    assert ranged["totals"]["savings"]["status"] == "estimated_range"
    assert ranged["totals"]["savings"]["amount_usd"] is None
    assert ranged["totals"]["savings"]["lower_bound_usd"] == "3.001"
    assert ranged["totals"]["savings"]["upper_bound_usd"] == "6.0015"
    trend = await _trend(client, auth)
    days = {day["date"]: day for day in trend["days"]}
    cloud = days["2026-10-09"]
    assert cloud["savings"]["amount_usd"] == "0"
    assert cloud["sources"]["third_party"]["coverage"]["total_tokens"] == "not_collected"
    missing = days["2026-10-11"]
    assert missing["sources"]["box"]["tokens"]["total_tokens"] == 11
    assert missing["sources"]["box"]["coverage"]["total_tokens"] == "partial"
    assert missing["sources"]["box"]["missing_records"]["total_tokens"] == 1
    assert missing["savings"]["status"] == "partial"
    assert trend["totals"]["sources"]["unattributed"]["tokens"]["total_tokens"] == 100
    assert trend["totals"]["savings"]["status"] == "partial"
    assert trend["totals"]["savings"]["amount_usd"] is None
    assert trend["totals"]["savings"]["upper_bound_usd"] is None
    assert trend["totals"]["savings"] == (await _summary(client, auth))["totals"]["savings"]
    unknown = await _trend(client, auth, task_id="missing-task")
    assert unknown["totals"]["savings"]["status"] == "unknown"
    assert unknown["totals"]["savings"]["known_amount_usd"] is None


async def test_trend_auth_scope_and_resource_filters(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    alice_auth = await create_user(client, auth, username="trend-alice")
    alice = await resolve_user_id(client, auth, "trend-alice")
    _task(server, owner, "admin-task", project_id="admin-project")
    _task(server, alice, "alice-task", project_id="alice-project")
    _call(server, owner, "admin", task_id="admin-task", usage=_tokens(10, 1))
    _call(server, alice, "alice", task_id="alice-task", usage=_tokens(20, 2))
    assert (await client.get("/api/usage/task-trend")).status_code == 401
    assert (await client.get("/api/admin/usage/task-trend", headers=alice_auth)).status_code == 403
    own = await _trend(client, alice_auth, user_id=owner)
    assert own["user_id"] == alice
    assert own["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 22
    global_trend = await _trend(client, auth, admin=True)
    assert global_trend["account_scope"] == "all_users" and global_trend["user_id"] is None
    assert global_trend["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 33
    assert (await _trend(client, auth, admin=True, user_id=alice))["totals"] == own["totals"]
    assert (await _trend(client, auth, admin=True, project_id="alice-project"))["totals"] == own[
        "totals"
    ]
    for filters in ({"task_id": "admin-task"}, {"project_id": "admin-project"}):
        empty = await _trend(client, alice_auth, **filters)
        assert empty["totals"]["savings"]["status"] == "no_data"
        assert all(day["sources"]["box"]["tokens"]["total_tokens"] is None for day in empty["days"])


@pytest.mark.parametrize(
    "timezone,date,hours",
    [("Asia/Shanghai", "2026-10-10", 24), ("America/New_York", "2026-11-01", 25)],
)
async def test_trend_server_calendar_and_half_open_boundaries(
    env: Any, monkeypatch: Any, timezone: str, date: str, hours: int
) -> None:
    client, server, auth = env
    monkeypatch.setattr(server, "config", replace(server.config, default_timezone=timezone))
    owner = await resolve_user_id(client, auth, "admin")
    start = int(datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(timezone)).timestamp() * 1000)
    end = start + hours * 3_600_000
    for index, timestamp in enumerate((start - 1, start, end - 1, end)):
        _call(
            server,
            owner,
            f"boundary-{index}",
            task_id="boundary-task",
            usage=_tokens(10, 1),
            started_at=timestamp,
        )
    trend = await _trend(client, auth, window=f"day:{date}")
    assert trend["timezone"] == timezone
    assert trend["range_start"] == start and trend["range_end"] == end
    assert len(trend["days"]) == 1 and trend["days"][0]["date"] == date
    assert trend["totals"]["sources"]["box"]["receipt_calls"] == 2
    assert trend["totals"]["sources"]["box"]["tokens"]["total_tokens"] == 22


@pytest.mark.parametrize(
    "window",
    [
        "all",
        "typo",
        "day:bad",
        "month:2026-13",
        "day:9999-12-31",
        "range:2026-10-10:2026-10-09",
        "range:2025-01-01:2026-01-02",
    ],
)
async def test_trend_rejects_invalid_or_unbounded_ranges(env: Any, window: str) -> None:
    client, _, auth = env
    response = await client.get(
        "/api/usage/task-trend",
        headers={**auth, "Accept-Language": "zh"},
        params={"window": window},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "USAGE_TREND_WINDOW_INVALID"
    assert "366 个自然日" in response.json()["error"]["message"]


async def test_trend_openapi_has_typed_chart_contract(tmp_inhere_box_home: Path) -> None:
    write_inhere_box_config(tmp_inhere_box_home, enable_api_docs=True)
    async with inhere_box_client(tmp_inhere_box_home) as (client, _):
        response = await client.get("/api/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        docs = await client.get("/api/docs")
        assert docs.status_code == 200 and "/api/openapi.json" in docs.text
    for path in ("/api/usage/task-trend", "/api/admin/usage/task-trend"):
        operation = schema["paths"][path]["get"]
        assert operation["summary"] and operation["description"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/TaskUsageTrend")
    assert "cumulative_savings" in schema["components"]["schemas"]["TaskTrendDay"]["properties"]
    assert "pricing" in schema["components"]["schemas"]["TaskUsageTrend"]["properties"]
