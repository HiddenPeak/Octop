"""Automatic Task failover preserves admission choices and records the execution route."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.agents.middleware import model_failover as middleware_module
from inhere_box.infra.agents.providers.health import ModelHealthRegistry
from inhere_box.infra.agents.providers.store import ProviderStore
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from inhere_box.infra.db.services import RepoBundle, SharedServices
from inhere_box.infra.projects import model_failover
from inhere_box.infra.projects.content import TaskContent
from inhere_box.infra.utils.paths import PathLayout


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path))
    db = SqlitePool(tmp_path / "failover.sqlite")
    run_migrations(db)
    repos = RepoBundle.from_pool(db)
    for name, kind, ids in (
        ("box", "box", ["local", "spare"]),
        ("cloud", "openai", ["remote"]),
    ):
        repos.provider_repo.create(
            name=name,
            kind=kind,
            api_key="" if kind == "box" else "test-secret",
            base_url=f"http://{name}.invalid/v1",
            models_json=json.dumps(
                [{"id": mid, "enabled": True, "context_window": 100_000} for mid in ids]
            ),
        )
    raw = {"execution_options": {}, "expert_profile": None}
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at) VALUES('task-one',1,'Work','request-one','turn-one',100,100)"
        )
        repos.project_task_turn_repo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="turn-one",
                task_id="task-one",
                request_id="turn-request",
                prompt="Continue",
                reply="",
                status="queued",
                error_code="",
                activity="",
                configuration_json=json.dumps(raw),
                model_ref="box/local",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    turn = repos.project_task_turn_repo.claim("worker-one", 1_000)
    services = SharedServices(PathLayout(tmp_path), InhereBoxConfig(), repos)
    models = {
        ref: SimpleNamespace(model=ref, profile={"max_input_tokens": 100_000})
        for ref in ("box/local", "box/spare", "cloud/remote")
    }
    health = ModelHealthRegistry()
    monkeypatch.setattr(model_failover, "model_health_for", lambda _repo: health)
    monkeypatch.setattr(
        model_failover,
        "InhereBoxChatModelFactory",
        lambda _providers: SimpleNamespace(get_chat_model=models.__getitem__, aclose=AsyncMock()),
    )
    monkeypatch.setattr(middleware_module, "_estimated_input_tokens", lambda _request: 100)
    try:
        yield services, turn, raw, models
    finally:
        db.close()


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({}, False),
        ({"execution_options": {}}, True),
        ({"execution_options": {"model_ref": "box/local"}}, False),
        (
            {
                "execution_options": {"model_ref": "cloud/text"},
                "capability_model_route": True,
            },
            True,
        ),
        (
            {
                "execution_options": {"model_ref": "cloud/text", "routing": {"mode": "box"}},
                "capability_model_route": True,
            },
            False,
        ),
        (
            {
                "execution_options": {"model_ref": "cloud/text"},
                "routing": {"target": "explicit"},
                "capability_model_route": True,
            },
            False,
        ),
        ({"execution_options": {}, "expert_profile": {"default_model": "box/local"}}, False),
        ({"execution_options": {}, "routing": {"target": "box"}}, True),
        ({"execution_options": {}, "routing": {"target": "explicit"}}, False),
        ({"execution_kind": "resident", "resident_configuration": {}}, False),
        (
            {
                "execution_kind": "resident",
                "resident_configuration": {"automatic_model_route": True},
            },
            True,
        ),
    ],
)
def test_only_recorded_automatic_routes_can_fail_over(configuration, expected):
    assert model_failover.automatic_route(configuration) is expected


def test_recovery_excludes_model_output_and_durable_tool_receipts(environment):
    services, turn, _, _ = environment
    recovery = {"phase": "model", "status_code": 503}
    content = TaskContent.restore(
        turn.turn_id,
        [
            {
                "block_id": "old-introduction",
                "type": "progress",
                "status": "completed",
                "tool_call_id": "narration:opening",
            }
        ],
        1,
    )
    assert model_failover.recovery_before_output(services, turn, recovery, content)
    assert not model_failover.recovery_before_output(
        services, replace(turn, reasoning="partial output"), recovery, content
    )
    with services.db.transaction() as conn:
        repo = services.project_task_execution_receipt_repo
        assert repo.start(
            conn,
            "receipt-one",
            turn.task_id,
            turn.turn_id,
            "call-one",
            "write_file",
            "digest",
            turn.worker_id,
            1_001,
        )
        assert repo.complete(conn, turn.turn_id, "call-one", turn.worker_id, "{}", 1_002)
    assert not model_failover.recovery_before_output(services, turn, recovery, content)


@pytest.mark.asyncio
async def test_box_failover_stays_local_and_audits_without_mutating_snapshot(
    environment, monkeypatch
):
    services, turn, raw, models = environment
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    middleware._retry.max_retries = 0
    configurable = {"model": "box/local", "inhere_box_auto_route": True}
    monkeypatch.setattr(middleware_module, "_configurable", lambda _request: configurable)
    attempted = []

    async def handler(request):
        attempted.append(request.model.model)
        if request.model.model == "box/local":
            raise ConnectionError("offline")
        # The route must already be durable before the replacement model is called.
        assert services.audit_repo.query(action="task.model_failover")
        return ModelResponse(result=[AIMessage(content="recovered")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")]),
        handler,
    )
    assert response.result[0].content == "recovered"
    assert attempted == ["box/local", "box/spare"]
    persisted = services.project_task_turn_repo.get(turn.turn_id)
    assert persisted.model_ref == "box/local"
    assert persisted.configuration_json == turn.configuration_json
    audit = services.audit_repo.query(action="task.model_failover")
    assert audit[0].target == turn.turn_id
    assert json.loads(audit[0].payload)["to_model_ref"] == "box/spare"
    await middleware.aclose()
    middleware._close_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_task_failure_is_raised_without_another_model(environment, monkeypatch):
    services, turn, raw, models = environment
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    middleware._retry.max_retries = 0
    monkeypatch.setattr(
        middleware_module,
        "_configurable",
        lambda _request: {"model": "box/local", "inhere_box_auto_route": False},
    )
    attempted = []

    async def handler(request):
        attempted.append(request.model.model)
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError, match="offline"):
        await middleware.awrap_model_call(
            ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")]),
            handler,
        )
    assert attempted == ["box/local"]
    assert not services.audit_repo.query(action="task.model_failover")


@pytest.mark.parametrize(
    ("mode", "level", "status", "explicit", "started", "has_checkpoint", "remote"),
    [
        ("hybrid", 100, 503, False, False, False, True),
        ("hybrid", 100, 500, False, False, False, False),
        ("hybrid", 0, 503, False, False, False, False),
        ("box", 0, 503, False, False, False, False),
        ("hybrid", 100, 503, True, False, False, False),
        ("hybrid", 100, 503, False, True, False, False),
        ("hybrid", 100, 503, False, False, True, False),
    ],
)
@pytest.mark.asyncio
async def test_box_503_can_fail_over_to_remote_on_fresh_hybrid_turns(
    environment, monkeypatch, mode, level, status, explicit, started, has_checkpoint, remote
):
    services, turn, raw, models = environment
    provider = services.provider_repo.get_by_name("box")
    services.provider_repo.update(
        provider.id, models_json=json.dumps([{"id": "local", "enabled": True}])
    )
    if has_checkpoint:
        turn = replace(turn, checkpoint_id="existing-checkpoint")
    raw["routing"] = {"mode": mode, "level": level, "target": "explicit" if explicit else "box"}
    if explicit:
        raw["execution_options"]["model_ref"] = "box/local"
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    initial_config = {
        "model": "box/local",
        "inhere_box_auto_route": model_failover.automatic_route(raw),
    }
    configurable = initial_config.copy()
    monkeypatch.setattr(middleware_module, "_configurable", lambda _request: configurable)
    request = ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")])
    attempted = []

    async def handler(routed):
        ref = routed.model.model
        attempted.append(ref)
        if ref == "box/local":
            if started:
                middleware.callbacks[0].on_llm_new_token("partial")
            response = httpx.Response(status, request=httpx.Request("POST", "http://box.invalid"))
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError("model call failed") from exc
        return ModelResponse(result=[AIMessage(content="recovered")])

    try:
        if remote:
            response = await middleware.awrap_model_call(request, handler)
            assert response.result[0].content == "recovered"
            configurable = initial_config.copy()
            await middleware.awrap_model_call(request, handler)
            assert attempted == ["box/local", "cloud/remote", "cloud/remote"]
            audit = services.audit_repo.query(action="task.model_failover")
            assert len(audit) == 1
            assert json.loads(audit[0].payload)["to_model_ref"] == "cloud/remote"
            assert services.project_task_turn_repo.get(turn.turn_id).model_ref == "box/local"
        else:
            with pytest.raises(RuntimeError, match="model call failed"):
                await middleware.awrap_model_call(request, handler)
            assert attempted == ["box/local"]
            assert not services.audit_repo.query(action="task.model_failover")
    finally:
        await middleware.aclose()


@pytest.mark.asyncio
async def test_failover_tries_newest_flagship_before_lightweight_model(environment, monkeypatch):
    services, turn, raw, models = environment
    provider = services.provider_repo.get_by_name("box")
    ids = ["local", "glm-4.7-flash", "glm-5.1", "glm-5.2"]
    services.provider_repo.update(
        provider.id,
        models_json=json.dumps([{"id": mid, "context_window": 100_000} for mid in ids]),
    )
    for mid in ids:
        models[f"box/{mid}"] = SimpleNamespace(
            model=f"box/{mid}", profile={"max_input_tokens": 100_000}
        )
    middleware = await model_failover.task_failover_middleware(
        services, 1, turn, raw, ProviderStore(services.provider_repo).build_harness_configs()
    )
    configurable = {"model": "box/local", "inhere_box_auto_route": True}
    monkeypatch.setattr(middleware_module, "_configurable", lambda _request: configurable)
    attempted = []

    async def handler(request):
        attempted.append(request.model.model)
        if request.model.model in {"box/local", "box/glm-5.2"}:
            raise ConnectionError("offline")
        return ModelResponse(result=[AIMessage(content="recovered")])

    try:
        response = await middleware.awrap_model_call(
            ModelRequest(model=models["box/local"], messages=[HumanMessage(content="continue")]),
            handler,
        )
        assert response.result[0].content == "recovered"
        assert attempted == ["box/local", "box/glm-5.2", "box/glm-5.1"]
    finally:
        await middleware.aclose()
