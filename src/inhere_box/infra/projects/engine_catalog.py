"""Owner-scoped execution engines and non-sensitive turn selections."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from inhere_box.infra.agents.acp_setup import ACPSetupService
from inhere_box.infra.agents.acp_setup_catalog import RUNNER_SETUP_SPECS
from inhere_box.infra.agents.settings.acp import ACPSettingsStore
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects.models import ProjectModel, Reference


class EngineSelection(ProjectModel):
    engine_id: Literal["inhere-box", "acp"] = "inhere-box"
    connection_id: Reference | None = Field(
        default=None, description="本用户的 ACP runner ID；inhere-box 引擎为 null。"
    )
    model_id: Reference | None = Field(
        default=None, description="连接的原生模型 ID；null 使用该引擎自己的默认模型。"
    )

    @model_validator(mode="before")
    @classmethod
    def read_legacy_engine(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("engine_id") == "octop":
            return {**value, "engine_id": "inhere-box"}
        return value

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        if self.engine_id == "inhere-box" and (self.connection_id or self.model_id):
            raise ValueError("inhere-box models use options.model_ref")
        if self.engine_id == "acp" and not self.connection_id:
            raise ValueError("ACP requires a connection_id")
        return self


class EngineCapabilities(ProjectModel):
    action_modes: list[Literal["ask", "plan", "craft"]] = ["craft"]
    images: bool = True
    permissions: bool = True
    native_resume: bool = True
    cancel: bool = True
    pause: bool = False
    skills: bool = True
    mcp: bool = True
    plugins: bool = False
    subagents: bool = False
    knowledge: bool = False
    memory: bool = True
    compact: bool = False
    model_selection: bool = True


class EngineModelOption(ProjectModel):
    model_id: str
    label: str


class ExecutionEngineSnapshot(EngineSelection):
    label: str = "inhere-box"
    capabilities: EngineCapabilities = Field(
        default_factory=lambda: EngineCapabilities(
            action_modes=["ask", "plan", "craft"],
            plugins=True,
            subagents=True,
            knowledge=True,
            compact=True,
            pause=True,
        )
    )
    config_digest: str | None = None
    workspace_scope: str | None = None
    actual_model_id: str | None = None
    resumed: bool | None = None


class EngineCatalogEntry(ProjectModel):
    engine: EngineSelection
    label: str
    available: bool
    unavailable_reason: str | None = None
    capabilities: EngineCapabilities
    config_digest: str | None = Field(
        default=None, description="连接启动配置摘要，不含启动参数或凭据。"
    )
    models: list[EngineModelOption] = Field(
        default_factory=list, description="原生模型 ID；空列表表示未发现列表，仍可使用默认模型。"
    )


def read_engine(execution_json: str) -> EngineSelection:
    document = json.loads(execution_json or "{}")
    return EngineSelection.model_validate(document.get("engine") or {})


def execution_json(selection: EngineSelection, previous: str = "{}") -> str:
    document = json.loads(previous or "{}")
    return json.dumps(
        {**document, "engine": selection.model_dump(), "sessions": document.get("sessions", {})}
    )


def catalog(
    services: SharedServices, owner_id: int, locale: str = "zh"
) -> list[EngineCatalogEntry]:
    """Read local files and saved checks only; never launch, install, or authenticate."""
    from inhere_box.infra.projects.tasks import resolve_model_ref

    available = resolve_model_ref(services) is not None
    entries = [
        EngineCatalogEntry(
            engine=EngineSelection(),
            label="inhere-box",
            available=available,
            unavailable_reason=None if available else "model_unavailable",
            capabilities=EngineCapabilities(
                action_modes=["ask", "plan", "craft"],
                plugins=True,
                subagents=True,
                knowledge=True,
                compact=True,
                pause=True,
            ),
        )
    ]
    from inhere_box.infra.projects.box_guest_policy import is_guest

    if is_guest(services, owner_id):
        entries[0].capabilities = entries[0].capabilities.model_copy(
            update={
                "skills": False,
                "mcp": False,
                "plugins": False,
                "subagents": False,
                "knowledge": False,
            }
        )
        return entries
    settings = ACPSettingsStore(
        settings_repo=services.settings_repo, agents_repo=services.agent_repo
    )
    setup = ACPSetupService(settings, services.paths.root)
    for runner, config in settings.load_runners(owner_id, migrate_legacy=False).items():
        status = setup._status(owner_id, runner, config, locale)
        observation = settings.load_setup_status(owner_id, runner)
        digest = _config_digest(config)
        capabilities = EngineCapabilities()
        models: list[EngineModelOption] = []
        if observation.get("fingerprint") == digest:
            discovered = observation.get("engine_capabilities")
            if isinstance(discovered, dict):
                capabilities = capabilities.model_copy(
                    update={
                        key: getattr(capabilities, key) and value
                        for key, value in discovered.items()
                        if key in EngineCapabilities.model_fields
                        and key != "action_modes"
                        and isinstance(value, bool)
                    }
                )
            raw_models = observation.get("engine_models")
            if isinstance(raw_models, list):
                models = [EngineModelOption.model_validate(item) for item in raw_models]
        reason = (
            "disabled"
            if not status.enabled
            else "not_installed"
            if status.installation != "installed"
            else status.connection
            if status.connection != "ready"
            else None
        )
        entries.append(
            EngineCatalogEntry(
                engine=EngineSelection(engine_id="acp", connection_id=runner),
                label=RUNNER_SETUP_SPECS[runner].name if runner in RUNNER_SETUP_SPECS else runner,
                available=reason is None,
                unavailable_reason=reason,
                capabilities=capabilities,
                config_digest=digest,
                models=models,
            )
        )
    return entries


def validate_engine(
    services: SharedServices,
    owner_id: int,
    selection: EngineSelection,
    *,
    action_mode: str = "craft",
) -> EngineCatalogEntry:
    entry = next(
        (
            item
            for item in catalog(services, owner_id)
            if item.engine.engine_id == selection.engine_id
            and item.engine.connection_id == selection.connection_id
        ),
        None,
    )
    if entry is None or not entry.available or action_mode not in entry.capabilities.action_modes:
        if selection.engine_id == "inhere-box" and entry is not None and not entry.available:
            raise InhereBoxError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE, status=409)
        raise InhereBoxError.localized(
            ErrorCode.TASK_SETTINGS_INVALID,
            status=409,
            setting="engine",
            details={
                "setting": "engine",
                "reason": entry.unavailable_reason if entry else "connection_unavailable",
            },
        )
    if selection.model_id is not None and selection.model_id not in {
        item.model_id for item in entry.models
    }:
        raise InhereBoxError.localized(
            ErrorCode.TASK_SETTINGS_INVALID,
            status=409,
            setting="engine.model_id",
            details={"setting": "engine.model_id", "reason": "model_unavailable"},
        )
    return entry.model_copy(update={"engine": selection})


def snapshot_engine(entry: EngineCatalogEntry) -> dict[str, Any]:
    return {
        **entry.engine.model_dump(),
        "label": entry.label,
        "capabilities": entry.capabilities.model_dump(),
        **({"config_digest": entry.config_digest} if entry.config_digest else {}),
    }


def validate_configuration(configuration: dict[str, Any]) -> None:
    """Reject unsupported inherited and explicit resources before queue admission."""
    engine = configuration.get("execution_engine") or {}
    if engine.get("engine_id") != "acp":
        return
    for capability, selected in (
        ("client_directory", configuration.get("client_directory")),
        ("plugins", configuration.get("plugins")),
        ("subagents", configuration.get("subagents") or configuration.get("team_enabled")),
        ("knowledge", configuration.get("knowledge_base_ids")),
        ("client_tools", configuration.get("execution_options", {}).get("client_tools")),
        (
            "tool_policy",
            (configuration.get("expert_profile") or {})
            .get("capabilities", {})
            .get("tools_disabled"),
        ),
    ):
        if selected:
            raise InhereBoxError.localized(
                ErrorCode.TASK_SETTINGS_INVALID,
                status=409,
                setting="engine",
                details={
                    "setting": "engine",
                    "reason": "unsupported_capability",
                    "capability": capability,
                },
            )


def connection_fingerprint(
    services: SharedServices, owner_id: int, selection: EngineSelection
) -> str:
    """Bind native sessions to this owner's exact launch configuration without exposing it."""
    settings = ACPSettingsStore(
        settings_repo=services.settings_repo, agents_repo=services.agent_repo
    )
    config = settings.load_runners(owner_id, migrate_legacy=False).get(
        selection.connection_id or "", {}
    )
    return _config_digest(config)


def _config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: config.get(key) for key in ("command", "args", "env")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
