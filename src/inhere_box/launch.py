"""Composition root — wire InhereBoxServer, FastAPI, and uvicorn for ``inhere-box run``."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from contextlib import ExitStack, suppress
from typing import Any

import uvicorn

from inhere_box.infra.agentcomputer.bootstrap import DaemonBootstrap
from inhere_box.infra.bonjour import BonjourPublisher
from inhere_box.infra.server import InhereBoxServer
from inhere_box.infra.utils.accept_errors import coalesce_accept_errors
from inhere_box.infra.utils.paths import PathLayout

logger = logging.getLogger(__name__)


def _ensure_linux_bubblewrap() -> None:
    """Best-effort startup provisioning; never fail the ``inhere-box run`` process."""
    try:
        from inhere_box.infra.utils.bwrap import ensure_bubblewrap

        result = ensure_bubblewrap()
    except Exception:
        logger.warning("background bubblewrap provisioning failed", exc_info=True)
        return
    status = result.get("status")
    detail = result.get("detail")
    if status == "degraded":
        logger.warning("background bubblewrap provisioning degraded: %s", detail or "unknown")
    else:
        logger.info("background bubblewrap provisioning status=%s", status)


def _schedule_linux_bubblewrap_ensure() -> asyncio.Task[None] | None:
    """Run bubblewrap provisioning once in the background on Linux."""
    if not sys.platform.startswith("linux"):
        return None
    return asyncio.create_task(
        asyncio.to_thread(_ensure_linux_bubblewrap),
        name="ensure-linux-bubblewrap",
    )


async def _cancel_background_task(task: asyncio.Task[None] | None) -> None:
    """Drop a startup background task on shutdown; never raise."""
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _serve(server: Any) -> None:
    await server.serve()


class _BonjourServer(uvicorn.Server):
    """Advertise the API only after uvicorn has bound a usable socket."""

    def __init__(self, config: uvicorn.Config, *, bonjour_enabled: bool) -> None:
        super().__init__(config)
        self._bonjour = BonjourPublisher() if bonjour_enabled else None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and self._bonjour is not None:
            # getsockname also resolves --port 0 to the actual listening port.
            host, port, *_ = self.servers[0].sockets[0].getsockname()
            await self._bonjour.start(host=host, port=port, tls=self.config.is_ssl)

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            if self._bonjour is not None:
                await self._bonjour.close()


async def run_foreground(
    *,
    host: str | None,
    port: int | None,
    reload: bool,
    workers: int,
    log_level: str | None,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    daemon_bootstrap: DaemonBootstrap | None = None,
) -> None:
    """Boot the domain server, serve the HTTP API, then shut down cleanly."""
    from inhere_box.infra.setup.branding_migration import require_installation_migrated

    paths = PathLayout.from_env()
    require_installation_migrated(paths.root)
    from inhere_box.api.app import build_app
    from inhere_box.infra.setup.tls.http_companion import build_http_companion_app
    from inhere_box.infra.setup.tls.listeners import build_listen_plan
    from inhere_box.infra.setup.tls.store import resolve_tls_paths

    guard = daemon_bootstrap or DaemonBootstrap(paths.agentcomputer_identity.parent)
    srv = InhereBoxServer(daemon_bootstrap=guard)
    bwrap_task: asyncio.Task[None] | None = None
    memory_control = None
    stack = ExitStack()
    try:
        stack.enter_context(coalesce_accept_errors(asyncio.get_running_loop()))
        await srv.start()
        bwrap_task = _schedule_linux_bubblewrap_ensure()
        # Greenfield deferral leaves services unset until /setup/database binds.
        cfg = srv.services.config if srv.services is not None else srv.config
        assert cfg is not None
        bind_host = host or cfg.bind_host
        bind_port = port if port is not None else cfg.port
        if ssl_certfile is None and ssl_keyfile is None:
            ssl_certfile, ssl_keyfile = resolve_tls_paths(srv.paths.root, cfg.tls)

        plan = build_listen_plan(
            cfg,
            bind_host=bind_host,
            bind_port=bind_port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
        level = (log_level or cfg.log_level).lower()
        worker_count = 1 if plan.dual_listeners or reload else workers
        if plan.dual_listeners and (reload or workers > 1):
            logger.warning(
                "TLS dual-port mode forces workers=1 and disables reload",
            )
            reload = False

        app = build_app(srv)
        servers: list[uvicorn.Server] = []

        if plan.dual_listeners:
            assert plan.https_port is not None
            assert plan.http_port is not None
            https_config = uvicorn.Config(
                app,
                host=plan.bind_host,
                port=plan.https_port,
                log_config=None,
                ws_max_size=32 * 1024 * 1024,
                log_level=level,
                workers=worker_count,
                reload=False,
                ssl_certfile=plan.ssl_certfile,
                ssl_keyfile=plan.ssl_keyfile,
            )
            companion = build_http_companion_app(https_port=plan.https_port)
            http_config = uvicorn.Config(
                companion,
                host=plan.bind_host,
                port=plan.http_port,
                log_config=None,
                ws_max_size=32 * 1024 * 1024,
                log_level=level,
                workers=1,
                reload=False,
            )
            servers.append(_BonjourServer(https_config, bonjour_enabled=cfg.bonjour_enabled))
            servers.append(uvicorn.Server(http_config))
            logger.info(
                "Listening on https://%s:%s and http://%s:%s (ACME + redirect)",
                plan.bind_host,
                plan.https_port,
                plan.bind_host,
                plan.http_port,
            )
        else:
            single_config = uvicorn.Config(
                app,
                host=plan.bind_host,
                port=plan.http_port if plan.http_port is not None else bind_port,
                log_config=None,
                ws_max_size=32 * 1024 * 1024,
                log_level=level,
                workers=worker_count,
                reload=reload,
                ssl_certfile=plan.ssl_certfile,
                ssl_keyfile=plan.ssl_keyfile,
            )
            servers.append(_BonjourServer(single_config, bonjour_enabled=cfg.bonjour_enabled))

        from inhere_box.infra.agents.memory.slim_control import MemorySlimControl

        memory_control = MemorySlimControl(srv)
        await memory_control.start()
        await asyncio.gather(*(_serve(s) for s in servers))
    finally:
        try:
            if memory_control is not None:
                await memory_control.close()
        finally:
            try:
                await _cancel_background_task(bwrap_task)
            finally:
                try:
                    await srv.stop()
                finally:
                    stack.close()


def run_foreground_blocking(**kwargs: Any) -> None:
    """Sync entry for Click commands and tests."""
    asyncio.run(run_foreground(**kwargs))
