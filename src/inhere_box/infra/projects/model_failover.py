"""Health-aware execution routes without changing the admitted turn snapshot."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from octop_harness.config import ProviderConfig

from inhere_box.infra.agents.middleware.model_failover import (
    ModelExecutionSafety,
    SafeModelFailoverMiddleware,
)
from inhere_box.infra.agents.middleware.reasoning import CONFIG_KEY
from inhere_box.infra.agents.providers.box import BoxProviderConfig, InhereBoxChatModelFactory
from inhere_box.infra.agents.providers.health import model_health_for
from inhere_box.infra.agents.providers.store import AutoRouteCandidate, ProviderStore
from inhere_box.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects.checkpoint_saver import checkpoint_fence
from inhere_box.infra.projects.content import TaskContent
from inhere_box.infra.projects.model_routing import REMOTE_THRESHOLDS
from inhere_box.infra.projects.tasks import TaskExecutionOptions, _reasoning_configuration


def automatic_route(configuration: dict[str, Any]) -> bool:
    """Only routes recorded as automatic may choose another model."""
    if configuration.get("execution_kind") == "resident":
        return configuration.get("resident_configuration", {}).get("automatic_model_route") is True
    options = configuration.get("execution_options")
    if not isinstance(options, dict):
        return False
    if (
        configuration.get("capability_model_route") is True
        and not options.get("routing")
        and not configuration.get("routing")
    ):
        return True
    if options.get("model_ref"):
        return False
    profile = configuration.get("expert_profile") or {}
    routing = configuration.get("routing") or {}
    return routing.get("target") != "explicit" and (
        bool(routing) or not profile.get("default_model")
    )


def recovery_before_output(
    services: SharedServices,
    turn: ProjectTaskTurnRow,
    recovery: dict[str, Any],
    content: TaskContent,
) -> bool:
    if (
        recovery.get("phase") != "model"
        or recovery.get("status_code") != 503
        or recovery.get("needs_reconciliation")
        or turn.reply
        or turn.reasoning
        or turn.pause_json
        or json.loads(turn.interactions_json)
        or any(
            block.type != "progress" or block.tool_call_id != "narration:opening"
            for block in content.blocks
        )
    ):
        return False
    with services.db.connect() as conn:
        return not (
            services.project_task_execution_receipt_repo.has_for_turn(conn, turn.turn_id)
            or services.project_task_tool_call_repo.has_for_turn(conn, turn.turn_id)
        )


async def task_failover_middleware(
    services: SharedServices,
    owner_id: int,
    turn: ProjectTaskTurnRow,
    configuration: dict[str, Any],
    provider_configs: list[ProviderConfig],
) -> SafeModelFailoverMiddleware:
    """Preserve Box boundaries, reasoning choices and an audit trail per route."""
    providers = ProviderStore(services.provider_repo)
    source = next(p for p in provider_configs if p.id == turn.model_ref.partition("/")[0])
    box_providers = {p.id for p in provider_configs if isinstance(p, BoxProviderConfig)}
    remote_providers = {p.id for p in provider_configs} - box_providers
    source_is_box = isinstance(source, BoxProviderConfig)
    same_source = box_providers if source_is_box else remote_providers
    routing = configuration.get("routing") or {}
    allow_remote_on_503 = (
        source_is_box
        and automatic_route(configuration)
        and routing.get("target") == "box"
        and routing.get("mode") == "hybrid"
        and routing.get("level") in REMOTE_THRESHOLDS
    )
    allowed = same_source | remote_providers if allow_remote_on_503 else same_source
    options = TaskExecutionOptions.model_validate(configuration.get("execution_options") or {})

    def catalog() -> tuple[list[AutoRouteCandidate], set[str], dict[str, dict[str, object]]]:
        compatible: list[AutoRouteCandidate] = []
        reasoning: dict[str, dict[str, object]] = {}
        for candidate in providers.list_auto_route_candidates(needs_multimodal=False):
            if candidate.ref.partition("/")[0] not in allowed:
                continue
            try:
                reasoning[candidate.ref] = _reasoning_configuration(
                    providers, candidate.ref, options
                )
            except InhereBoxError:
                continue
            compatible.append(candidate)
        vision = {
            candidate.ref
            for candidate in providers.list_auto_route_candidates(needs_multimodal=True)
        }
        return compatible, vision, reasoning

    candidates, vision, reasoning = await asyncio.to_thread(catalog)
    factory = InhereBoxChatModelFactory(provider_configs)
    current_ref = turn.model_ref

    async def selected(ref: str, configurable: dict[str, Any]) -> None:
        nonlocal current_ref
        if ref == current_ref:
            configurable[CONFIG_KEY] = reasoning[ref].get("reasoning_overrides", {})
            return
        previous_ref = current_ref

        def record() -> None:
            with checkpoint_fence(services, turn):
                with services.db.transaction() as conn:
                    if not services.project_task_repo.lock_owner(conn, owner_id):
                        raise InhereBoxError.localized(ErrorCode.FORBIDDEN)
                    row = services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
                    if (
                        row is None
                        or row.status != "running"
                        or row.worker_id != turn.worker_id
                        or row.cancel_requested
                    ):
                        raise InhereBoxError.localized(ErrorCode.TASK_INTERRUPTED)
                services.audit_repo.user_event(
                    str(owner_id),
                    "task.model_failover",
                    turn.turn_id,
                    json.dumps(
                        {
                            "task_id": turn.task_id,
                            "planned_model_ref": turn.model_ref,
                            "from_model_ref": previous_ref,
                            "to_model_ref": ref,
                        }
                    ),
                )

        await asyncio.to_thread(record)
        current_ref = ref
        configurable[CONFIG_KEY] = reasoning[ref].get("reasoning_overrides", {})

    return SafeModelFailoverMiddleware(
        health=model_health_for(services.provider_repo),
        candidates=lambda _current, needs_multimodal: [
            candidate
            for candidate in candidates
            if candidate.ref.partition("/")[0] in same_source
            and (not needs_multimodal or candidate.ref in vision)
        ],
        service_unavailable_candidates=(
            lambda _current, needs_multimodal: [
                candidate
                for candidate in candidates
                if candidate.ref.partition("/")[0] in remote_providers
                and (not needs_multimodal or candidate.ref in vision)
            ]
        )
        if allow_remote_on_503
        else None,
        model_for_ref=factory.get_chat_model,
        max_retries=0,
        initial_delay=1,
        max_delay=4,
        on_failure="error",
        on_model_selected=selected,
        close_models=factory.aclose,
        execution_safety=ModelExecutionSafety(
            allowed=not bool(turn.checkpoint_id or turn.reply or turn.recovery_json)
        ),
    )
