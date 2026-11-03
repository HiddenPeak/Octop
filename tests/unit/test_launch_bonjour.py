"""The advertised endpoint comes from the ready API listener, including port 0."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn

from inhere_box import launch


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("tls", [False, True])
async def test_publish_only_after_successful_socket_startup(monkeypatch, enabled, started, tls):
    publisher = AsyncMock()
    monkeypatch.setattr(launch, "BonjourPublisher", lambda: publisher)
    config = uvicorn.Config(
        "unused:app",
        host="0.0.0.0",
        port=0,
        ssl_certfile="fixture-cert" if tls else None,
        ssl_keyfile="fixture-key" if tls else None,
    )
    server = launch._BonjourServer(config, bonjour_enabled=enabled)

    async def startup(self, sockets=None):
        publisher.start.assert_not_called()
        self.started = started
        self.servers = [
            SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ("0.0.0.0", 49152))])
        ]

    async def serve(self, sockets=None):
        await self.startup(sockets=sockets)

    monkeypatch.setattr(uvicorn.Server, "startup", startup)
    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await server.serve()
    if enabled and started:
        publisher.start.assert_awaited_once_with(host="0.0.0.0", port=49152, tls=tls)
    else:
        publisher.start.assert_not_called()
    assert publisher.close.await_count == int(enabled)


async def test_serve_exception_still_withdraws_advertisement(monkeypatch):
    publisher = AsyncMock()
    monkeypatch.setattr(launch, "BonjourPublisher", lambda: publisher)

    async def fail(self, sockets=None):
        raise RuntimeError("serve failed")

    monkeypatch.setattr(uvicorn.Server, "serve", fail)
    server = launch._BonjourServer(uvicorn.Config("unused:app"), bonjour_enabled=True)
    with pytest.raises(RuntimeError, match="serve failed"):
        await server.serve()
    publisher.close.assert_awaited_once()


@pytest.mark.parametrize("tls", [False, True])
async def test_dual_listeners_only_publish_main_api(tmp_inhere_box_home, monkeypatch, tls):
    from inhere_box.infra.setup.tls.listeners import ListenPlan

    configs = []
    served = []

    def plan(cfg, **kwargs):
        assert kwargs["bind_port"] == 0  # CLI port 0 must reach uvicorn unchanged.
        return ListenPlan(
            bind_host="0.0.0.0",
            https_port=8088 if tls else None,
            http_port=8080 if tls else 0,
            ssl_certfile="fixture-cert" if tls else None,
            ssl_keyfile="fixture-key" if tls else None,
        )

    original = uvicorn.Config

    def config(*args, **kwargs):
        configs.append(kwargs)
        return original(*args, **kwargs)

    async def serve(server):
        served.append(server)

    monkeypatch.setattr("inhere_box.infra.setup.tls.listeners.build_listen_plan", plan)
    monkeypatch.setattr(uvicorn, "Config", config)
    monkeypatch.setattr(launch, "_schedule_linux_bubblewrap_ensure", lambda: None)
    monkeypatch.setattr(launch, "_serve", serve)
    await launch.run_foreground(
        host="0.0.0.0",
        port=0,
        reload=False,
        workers=1,
        log_level="warning",
        ssl_certfile=None,
        ssl_keyfile=None,
    )
    assert isinstance(served[0], launch._BonjourServer)
    assert configs[0]["port"] == (8088 if tls else 0)
    if tls:
        assert len(served) == 2
        assert not isinstance(served[1], launch._BonjourServer)
        assert configs[1]["port"] == 8080
    else:
        assert len(served) == 1
