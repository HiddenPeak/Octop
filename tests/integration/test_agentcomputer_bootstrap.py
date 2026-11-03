"""Independent task servers retain daemon ownership through runtime cleanup."""

from __future__ import annotations

import asyncio
import sqlite3
from threading import Event

import pytest

from inhere_box.infra.agentcomputer.bootstrap import DaemonBootstrap
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.server import InhereBoxServer
from tests.support.app import ensure_control_plane_bound, write_inhere_box_config


async def test_second_daemon_refuses_same_home_but_embedded_can_start(tmp_inhere_box_home):
    first = InhereBoxServer(
        home=tmp_inhere_box_home,
        daemon_bootstrap=DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control"),
    )
    second = InhereBoxServer(
        home=tmp_inhere_box_home,
        daemon_bootstrap=DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control"),
    )
    embedded = InhereBoxServer(home=tmp_inhere_box_home)
    try:
        await first.start()
        with pytest.raises(InhereBoxError) as caught:
            await second.start()
        assert caught.value.code == ErrorCode.CONTROL_PLANE_BUSY
        await second.stop()
        await embedded.start()
        await embedded.stop()
        with pytest.raises(InhereBoxError):
            await second.start()
    finally:
        await first.stop()
        await embedded.stop()
    await second.start()
    await second.stop()


@pytest.mark.parametrize("component", ["gateway", "cron", "registry", "users", "proactive"])
async def test_partial_runtime_boot_closes_created_components_before_releasing_guard(
    tmp_inhere_box_home, monkeypatch, component
):
    from inhere_box.infra.agents.manager import AgentManager
    from inhere_box.infra.cron.manager import CronManager
    from inhere_box.infra.gateway.gateway import Gateway
    from inhere_box.infra.proactive.scheduler import ProactiveCareScheduler
    from inhere_box.infra.users.manager import UserManager

    write_inhere_box_config(
        tmp_inhere_box_home, database={"driver": "sqlite", "sqlite_path": "inhere-box.db"}
    )
    pool = SqlitePool(tmp_inhere_box_home / "inhere-box.db")
    run_migrations(pool)
    pool.close()
    server = InhereBoxServer(
        home=tmp_inhere_box_home,
        daemon_bootstrap=DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control"),
    )
    lifecycle = {
        "gateway": (Gateway, "boot", "shutdown"),
        "cron": (CronManager, "boot", "shutdown"),
        "registry": (AgentManager, "boot", "shutdown"),
        "users": (UserManager, "boot", "shutdown_all"),
        "proactive": (ProactiveCareScheduler, "start_all", "shutdown"),
    }
    closed = []
    opened = []
    for name, (kind, _, shutdown_name) in lifecycle.items():
        original = getattr(kind, shutdown_name)

        def make_close(label, method):
            async def close(instance):
                await method(instance)
                closed.append(label)

            return close

        monkeypatch.setattr(kind, shutdown_name, make_close(name, original))

    async def fail(_instance):
        assert server.services is not None
        opened.append(server.services.db)
        raise RuntimeError("fixture partial boot failure")

    kind, boot_name, _ = lifecycle[component]
    monkeypatch.setattr(kind, boot_name, fail)
    with pytest.raises(RuntimeError, match="partial boot"):
        await server.start()
        await ensure_control_plane_bound(server)
    assert component in closed and "registry" in closed
    assert server.services is None and server.app_runtime is None
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError), opened[0].connect() as conn:
        conn.execute("SELECT 1")
    other = DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control")
    await other.acquire()
    await other.close()
    await server.stop()


@pytest.mark.parametrize("operation", ["stop", "startFailure"])
async def test_pool_close_is_settled_despite_repeated_cancel_before_guard_release(
    tmp_inhere_box_home, monkeypatch, operation
):
    write_inhere_box_config(
        tmp_inhere_box_home, database={"driver": "sqlite", "sqlite_path": "inhere-box.db"}
    )
    pool = SqlitePool(tmp_inhere_box_home / "inhere-box.db")
    run_migrations(pool)
    pool.close()
    server = InhereBoxServer(
        home=tmp_inhere_box_home,
        daemon_bootstrap=DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control"),
    )
    closing, release, finished = Event(), Event(), Event()

    def install_delayed_close():
        assert server.services is not None
        original = server.services.db.close

        def delayed():
            closing.set()
            assert release.wait(5)
            original()
            finished.set()

        monkeypatch.setattr(server.services.db, "close", delayed)

    if operation == "stop":
        await server.start()
        await ensure_control_plane_bound(server)
        install_delayed_close()
        task = asyncio.create_task(server.stop())
    else:

        def fail():
            install_delayed_close()
            raise RuntimeError("fixture JWT startup failure")

        monkeypatch.setattr(server, "_ensure_jwt_secret", fail)
        task = asyncio.create_task(server.start())
    other = DaemonBootstrap(tmp_inhere_box_home / "agentcomputer-control")
    try:
        assert await asyncio.to_thread(closing.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(InhereBoxError):
            await other.acquire()
        assert not finished.is_set() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert server.services is None and server.app_runtime is None
    finally:
        release.set()
        await server.stop()
    await other.acquire()
    await other.close()
