"""AppRuntime.replace_services — public control-plane retarget API."""

from __future__ import annotations

from pathlib import Path

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.agents.manager import AgentManager
from inhere_box.infra.cron.delivery import CronDeliveryService
from inhere_box.infra.cron.manager import CronManager
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.services import build_shared_services
from inhere_box.infra.gateway.gateway import Gateway
from inhere_box.infra.proactive.scheduler import ProactiveCareScheduler
from inhere_box.infra.proactive.service import ProactiveCareService
from inhere_box.infra.server import AppRuntime
from inhere_box.infra.users.manager import UserManager
from inhere_box.infra.utils.paths import PathLayout


def _services(tmp_path: Path, name: str):
    paths = PathLayout(tmp_path / name)
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    cfg = InhereBoxConfig()
    return build_shared_services(db=db, paths=paths, config=cfg), cfg, paths


def test_replace_services_retargets_user_and_provider_repos(tmp_path: Path) -> None:
    svc_a, cfg_a, paths_a = _services(tmp_path, "a")
    svc_b, cfg_b, _paths_b = _services(tmp_path, "b")

    registry = AgentManager(repos=svc_a.repos, paths=paths_a, config=cfg_a)
    gateway = Gateway(agent_manager=registry, repos=svc_a.repos)
    cron = CronManager(
        gateway=gateway,
        delivery_service=CronDeliveryService(
            gateway=gateway,
            agent_manager=registry,
            repos=svc_a.repos,
        ),
        repos=svc_a.repos,
        timezone=cfg_a.default_timezone,
    )
    care = ProactiveCareService(
        gateway=gateway,
        care_push_repo=svc_a.repos.care_push_repo,
        agent_manager=registry,
        timezone=cfg_a.default_timezone,
    )
    sched = ProactiveCareScheduler(
        care_service=care,
        config_repo=svc_a.repos.proactive_care_config_repo,
        session_repo=svc_a.repos.session_repo,
    )
    users = UserManager(svc_a)
    rt = AppRuntime(
        agent_registry=registry,
        gateway=gateway,
        cron_manager=cron,
        user_manager=users,
        proactive_scheduler=sched,
    )

    rt.replace_services(svc_b, cfg_b)

    assert users._services is svc_b  # noqa: SLF001 — assert retarget landed
    assert registry.providers._provider_repo._db is svc_b.db  # noqa: SLF001
    assert gateway._repos is svc_b.repos  # noqa: SLF001
    assert cron._repos is svc_b.repos  # noqa: SLF001
    assert cron._delivery_service._repos is svc_b.repos  # noqa: SLF001


def test_rebind_module_delegates_retarget_to_app_runtime() -> None:
    """db.rebind must not own runtime private-field surgery."""
    src = Path(__file__).resolve().parents[3] / "src/inhere_box/infra/db/rebind.py"
    text = src.read_text(encoding="utf-8")
    assert "replace_services" in text
    assert "_retarget_runtime" not in text
    assert "registry._repos" not in text
