"""Daily task usage and cumulative reference savings for the Tokens charts."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from octop.infra.db.repos.model_calls import TOKEN_FIELDS, AttributedTaskModelCall
from octop.infra.db.repos.usage import RetainedTaskUsageEntry, resolve_usage_window
from octop.infra.usage.pricing import (
    LONG_CONTEXT_RATES,
    SHORT_CONTEXT_RATES,
    ReferenceCost,
    compute_reference_cost,
)
from octop.infra.usage.task_summary import (
    ReferenceCostAmounts,
    ReferencePricing,
    TaskTokenCounts,
    TaskTokenCoverage,
    TaskTokenTotals,
    TaskUsageCapabilities,
    TokenCoverage,
    _cost_totals,
    _rates,
    _savings,
)

if TYPE_CHECKING:
    from octop.infra.db.services import SharedServices


class TaskTrendFilters(BaseModel):
    window: str = Field(
        default="last_30d",
        max_length=64,
        description="today/yesterday/last_7d/last_30d/day:YYYY-MM-DD/month:YYYY-MM/"
        "range:YYYY-MM-DD:YYYY-MM-DD. At most 366 calendar dates; all is not supported.",
    )
    task_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)


class TrendTokenUsage(BaseModel):
    tokens: TaskTokenTotals
    coverage: TaskTokenCoverage
    missing_records: TaskTokenCounts
    receipt_calls: int
    historical_records: int = Field(
        description="Selected journal records, attributed by historical configuration inference."
    )


class TrendSources(BaseModel):
    box: TrendTokenUsage
    third_party: TrendTokenUsage
    unattributed: TrendTokenUsage = Field(
        description="Historical records with unknown provider kind; never silently assigned."
    )


class TaskTrendTotals(BaseModel):
    sources: TrendSources
    savings: ReferenceCostAmounts


class TaskTrendDay(TaskTrendTotals):
    date: str = Field(description="Calendar date YYYY-MM-DD in the response timezone.")
    cumulative_savings: ReferenceCostAmounts = Field(
        description="Savings from the query start through this date; includes unknowns."
    )


class TaskUsageTrend(BaseModel):
    window: str
    range_start: int = Field(description="Inclusive Unix milliseconds.")
    range_end: int = Field(description="Exclusive Unix milliseconds.")
    timezone: str
    generated_at: int = Field(description="Unix milliseconds; not a frozen accounting snapshot.")
    account_scope: Literal["user", "all_users"]
    user_id: int | None
    recording_scope: Literal["retained_records_only"] = "retained_records_only"
    history_complete: Literal[False] = False
    selection_basis: Literal["per_task_receipts_precede_journals"] = (
        "per_task_receipts_precede_journals"
    )
    capabilities: TaskUsageCapabilities = Field(default_factory=TaskUsageCapabilities)
    pricing: ReferencePricing
    excluded_unassigned_calls: int
    totals: TaskTrendTotals
    days: list[TaskTrendDay] = Field(
        description="Ascending calendar dates, including empty dates; no pagination or truncation."
    )


def _tokens(
    calls: Sequence[AttributedTaskModelCall],
    entries: Sequence[RetainedTaskUsageEntry],
    usages: dict[str, dict[str, object]],
) -> TrendTokenUsage:
    values: dict[str, int | None] = {}
    missing: dict[str, int] = {}
    coverage: dict[str, TokenCoverage] = {}
    count = len(calls) + len(entries)
    for field in TOKEN_FIELDS:
        raw = [usages[call.row.call_id].get(field) for call in calls]
        raw.extend(getattr(entry, field, None) for entry in entries)
        known = [value for value in raw if type(value) is int]
        values[field] = sum(known) if known else None
        missing[field] = count - len(known)
        coverage[field] = (
            "no_data"
            if not count
            else "not_collected"
            if not known
            else "partial"
            if missing[field]
            else "recorded"
        )
    return TrendTokenUsage(
        tokens=TaskTokenTotals.model_validate(values),
        coverage=TaskTokenCoverage.model_validate(coverage),
        missing_records=TaskTokenCounts.model_validate(missing),
        receipt_calls=len(calls),
        historical_records=len(entries),
    )


def _sources(
    calls: Sequence[AttributedTaskModelCall],
    entries: Sequence[RetainedTaskUsageEntry],
    usages: dict[str, dict[str, object]],
) -> TrendSources:
    return TrendSources(
        box=_tokens(
            [call for call in calls if call.row.source == "box"],
            [entry for entry in entries if entry.provider_kind == "box"],
            usages,
        ),
        third_party=_tokens(
            [call for call in calls if call.row.source == "third_party"],
            [entry for entry in entries if entry.provider_kind not in {None, "box"}],
            usages,
        ),
        unattributed=_tokens(
            [], [entry for entry in entries if entry.provider_kind is None], usages
        ),
    )


def _sum_savings(parts: Sequence[ReferenceCostAmounts]) -> ReferenceCostAmounts:
    return ReferenceCostAmounts.model_validate(
        _cost_totals([part for part in parts if part.status != "no_data"]).model_dump()
    )


def _task_trend(
    services: SharedServices, *, user_id: int | None, filters: TaskTrendFilters, timezone: str
) -> TaskUsageTrend:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    if filters.window not in {"today", "yesterday", "last_7d", "last_30d"} and not (
        filters.window.startswith(("day:", "month:", "range:"))
    ):
        raise ValueError("Invalid daily usage window; use a bounded date range")
    start, end = resolve_usage_window(filters.window, timezone=tz.key)
    first_date = datetime.fromtimestamp(start, tz).date()
    last_date = datetime.fromtimestamp(end - 1, tz).date()
    day_count = (last_date - first_date).days + 1
    if not 1 <= day_count <= 366:
        raise ValueError("Daily usage window must contain 1 to 366 calendar dates")

    all_calls = services.model_call_repo.list_task_usage(
        user_id=user_id,
        range_start=start * 1000,
        range_end=end * 1000,
        task_id=filters.task_id,
        project_id=filters.project_id,
    )
    _, retained_entries = services.usage_repo.read_retained_task_usage(
        user_id=user_id,
        range_start=start,
        range_end=end,
        task_id=filters.task_id,
        project_id=filters.project_id,
    )
    calls = [call for call in all_calls if call.task_id]
    tasks_with_calls = {call.task_id for call in calls}
    # Select once for the entire window, before bucketing. Daily fallback would double count.
    entries = [entry for entry in retained_entries if entry.task_id not in tasks_with_calls]
    usages = {call.row.call_id: json.loads(call.row.usage_json) for call in calls}
    costs: dict[str, ReferenceCost] = {
        key: compute_reference_cost(usage) for key, usage in usages.items()
    }
    calls_by_day: dict[str, list[AttributedTaskModelCall]] = defaultdict(list)
    entries_by_day: dict[str, list[RetainedTaskUsageEntry]] = defaultdict(list)
    for call in calls:
        calls_by_day[
            datetime.fromtimestamp(call.row.started_at / 1000, tz).date().isoformat()
        ].append(call)
    for entry in entries:
        entries_by_day[datetime.fromtimestamp(entry.ts, tz).date().isoformat()].append(entry)

    days: list[TaskTrendDay] = []
    savings_parts: list[ReferenceCostAmounts] = []
    for offset in range(day_count):
        date = (first_date + timedelta(days=offset)).isoformat()
        day_calls, day_entries = calls_by_day[date], entries_by_day[date]
        # Different tasks may contribute receipts and journals on the same date.
        savings = _sum_savings(
            [
                _savings(day_calls, [], costs),
                _savings([], day_entries, costs),
            ]
        )
        savings_parts.append(savings)
        days.append(
            TaskTrendDay(
                date=date,
                sources=_sources(day_calls, day_entries, usages),
                savings=savings,
                cumulative_savings=_sum_savings(savings_parts),
            )
        )
    return TaskUsageTrend(
        window=filters.window,
        range_start=start * 1000,
        range_end=end * 1000,
        timezone=tz.key,
        generated_at=time.time_ns() // 1_000_000,
        account_scope="all_users" if user_id is None else "user",
        user_id=user_id,
        pricing=ReferencePricing(
            short_context=_rates(SHORT_CONTEXT_RATES),
            long_context=_rates(LONG_CONTEXT_RATES),
        ),
        excluded_unassigned_calls=len(all_calls) - len(calls),
        totals=TaskTrendTotals(
            sources=_sources(calls, entries, usages), savings=_sum_savings(savings_parts)
        ),
        days=days,
    )


async def task_usage_trend(
    services: SharedServices, *, user_id: int | None, filters: TaskTrendFilters, timezone: str
) -> TaskUsageTrend:
    return await asyncio.to_thread(
        _task_trend, services, user_id=user_id, filters=filters, timezone=timezone
    )
