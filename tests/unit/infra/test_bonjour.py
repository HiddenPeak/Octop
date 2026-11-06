"""Bonjour advertises reachable listener addresses and never owns HTTP availability."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import ifaddr
import pytest
from zeroconf import IPVersion

from inhere_box import __version__
from inhere_box.infra import bonjour


@pytest.fixture
def adapters(monkeypatch):
    entries = [
        ifaddr.Adapter("loopback", "loopback", [ifaddr.IP("127.0.0.1", 8, "lo")], index=1),
        ifaddr.Adapter(
            "lan",
            "lan",
            [
                ifaddr.IP("192.168.50.10", 24, "lan"),
                ifaddr.IP(("fe80::10", 0, 2), 64, "lan"),
            ],
            index=2,
        ),
        ifaddr.Adapter("other", "other", [ifaddr.IP("10.0.0.5", 24, "other")], index=3),
    ]
    monkeypatch.setattr(bonjour.ifaddr, "get_adapters", lambda: entries)
    return entries


@pytest.mark.parametrize(
    ("host", "addresses", "interfaces"),
    [
        ("0.0.0.0", ["192.168.50.10", "10.0.0.5"], ["192.168.50.10", "10.0.0.5"]),
        ("192.168.50.10", ["192.168.50.10"], ["192.168.50.10"]),
        ("127.0.0.1", [], []),
        ("::", ["fe80::10"], [2]),
        ("::1", [], []),
    ],
)
def test_only_bound_lan_interfaces_are_advertised(adapters, host, addresses, interfaces):
    assert bonjour._bound_addresses(host) == (addresses, interfaces)


@pytest.mark.parametrize("tls", [False, True])
async def test_registration_contains_client_contract_and_closes(monkeypatch, adapters, tls):
    announcements = AsyncMock()
    zeroconf = AsyncMock()
    zeroconf.async_register_service.side_effect = lambda *a, **kw: announcements()
    constructions = []

    def create(**kwargs):
        constructions.append(kwargs)
        return zeroconf

    monkeypatch.setattr(bonjour, "AsyncZeroconf", create)
    monkeypatch.setattr(bonjour.socket, "gethostname", lambda: "Box-" + "盒" * 50)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=49152, tls=tls)
    info = zeroconf.async_register_service.call_args.args[0]
    assert info.type == "_inhere-box._tcp.local."
    assert info.port == 49152
    assert info.parsed_addresses() == ["192.168.50.10", "10.0.0.5"]
    assert info.properties == {
        b"txtvers": b"1",
        b"product": b"inhere-box",
        b"version": __version__.encode(),
        b"scheme": b"https" if tls else b"http",
        b"path": b"/",
    }
    assert len(info.name.removesuffix("." + info.type).encode()) <= 63
    assert len(info.server.split(".")[0].encode()) <= 63
    assert constructions == [
        {"interfaces": ["192.168.50.10", "10.0.0.5"], "ip_version": IPVersion.V4Only}
    ]
    assert zeroconf.async_register_service.call_args.kwargs == {"allow_name_change": True}
    announcements.assert_awaited_once()
    await publisher.close()
    await publisher.close()
    zeroconf.async_close.assert_awaited_once()


async def test_loopback_does_not_open_multicast_sockets(monkeypatch, adapters):
    def unexpected(**kwargs):
        pytest.fail("loopback must not be advertised")

    monkeypatch.setattr(bonjour, "AsyncZeroconf", unexpected)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="127.0.0.1", port=8088, tls=False)
    await publisher.close()


async def test_registration_failure_is_nonfatal_and_cleans_sockets(monkeypatch, adapters):
    zeroconf = AsyncMock()
    zeroconf.async_register_service.side_effect = OSError("multicast blocked")
    monkeypatch.setattr(bonjour, "AsyncZeroconf", lambda **kw: zeroconf)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    zeroconf.async_close.assert_awaited_once()
    await publisher.close()
    zeroconf.async_close.assert_awaited_once()


async def test_interface_failure_does_not_prevent_service_start(monkeypatch):
    def fail(host):
        raise OSError("interface enumeration unavailable")

    monkeypatch.setattr(bonjour, "_bound_addresses", fail)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    await publisher.close()


async def test_cleanup_failure_does_not_interrupt_shutdown(monkeypatch, adapters):
    zeroconf = AsyncMock()
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    zeroconf.async_register_service.return_value = future
    zeroconf.async_close.side_effect = OSError("interface disappeared")
    monkeypatch.setattr(bonjour, "AsyncZeroconf", lambda **kw: zeroconf)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    await publisher.close()
    zeroconf.async_close.assert_awaited_once()


@pytest.fixture
def registrations(monkeypatch):
    created = []

    def create(**kwargs):
        zeroconf = AsyncMock()
        announcements = AsyncMock()
        zeroconf.async_register_service.side_effect = lambda *a, **kw: announcements()
        created.append((kwargs, zeroconf))
        return zeroconf

    monkeypatch.setattr(bonjour, "AsyncZeroconf", create)
    return created


@pytest.mark.parametrize(
    ("host", "before", "after", "ip_version"),
    [
        (
            "0.0.0.0",
            (["192.168.0.21"], ["192.168.0.21"]),
            (["192.168.3.90"], ["192.168.3.90"]),
            IPVersion.V4Only,
        ),
        ("::", (["fe80::10"], [2]), (["fe80::10"], [3]), IPVersion.V6Only),
    ],
)
async def test_network_change_rebinds_sockets_and_preserves_identity(
    monkeypatch, registrations, host, before, after, ip_version
):
    monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: before)
    publisher = bonjour.BonjourPublisher()
    box_id = "f095fb9a-7f69-4d01-8d85-63bea1356b8e"
    await publisher.start(host=host, port=8088, tls=False, box_id=box_id)
    first = registrations[0][1]
    original = first.async_register_service.call_args.args[0]
    # Zeroconf may rename a conflicting instance during registration.
    original.name = f"renamed.{bonjour.SERVICE_TYPE}"
    try:
        await publisher._refresh(host)
        assert len(registrations) == 1  # Unchanged networks do not churn registrations.
        monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: after)
        await publisher._refresh(host)
        first.async_close.assert_awaited_once()
        kwargs, second = registrations[1]
        assert kwargs == {"interfaces": after[1], "ip_version": ip_version}
        updated = second.async_register_service.call_args.args[0]
        assert updated.parsed_addresses() == after[0]
        assert updated.server == original.server == f"inhere-box-{box_id.replace('-', '')}.local."
        assert updated.name == original.name
        assert updated.port == original.port
        assert updated.properties == original.properties
    finally:
        await publisher.close()
    second.async_close.assert_awaited_once()


async def test_multicast_recovery_with_unchanged_addresses(monkeypatch, registrations):
    monkeypatch.setattr(
        bonjour, "_bound_addresses", lambda host: (["192.168.0.21"], ["192.168.0.21"])
    )
    probe = AsyncMock(side_effect=[False, True, False, False, False])
    monkeypatch.setattr(bonjour, "_advertisement_healthy", probe)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    first = registrations[0][1]
    original = first.async_register_service.call_args.args[0]
    try:
        await publisher._refresh("0.0.0.0")
        probe.assert_not_awaited()
        for _ in range(4):
            publisher._probe_at = 0.0
            await publisher._refresh("0.0.0.0")
            first.async_close.assert_not_awaited()
        await publisher._refresh("0.0.0.0")
        first.async_close.assert_awaited_once()
        second = registrations[1][1]
        restored = second.async_register_service.call_args.args[0]
        assert (restored.name, restored.server, restored.port) == (
            original.name,
            original.server,
            original.port,
        )
        assert restored.parsed_addresses() == original.parsed_addresses()
        await publisher._refresh("0.0.0.0")
        assert probe.await_count == 5
        assert len(registrations) == 2
    finally:
        await publisher.close()


@pytest.mark.parametrize("initially_offline", [False, True])
async def test_offline_and_recovery(monkeypatch, registrations, initially_offline):
    network = ([], []) if initially_offline else (["192.168.0.21"], ["192.168.0.21"])
    monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: network)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    try:
        server = publisher._info.server
        network = ([], [])
        await publisher._refresh("0.0.0.0")
        if not initially_offline:
            registrations[0][1].async_close.assert_awaited_once()
        assert publisher._zeroconf is None
        network = (["192.168.3.90"], ["192.168.3.90"])
        await publisher._refresh("0.0.0.0")
        info = registrations[-1][1].async_register_service.call_args.args[0]
        assert info.parsed_addresses() == ["192.168.3.90"]
        assert info.server == server  # Also stable for pre-setup discovery.
    finally:
        await publisher.close()


@pytest.mark.parametrize("failure", ["enumeration", "registration"])
async def test_network_failure_is_retried(monkeypatch, registrations, failure):
    network = (["192.168.0.21"], ["192.168.0.21"])
    monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: network)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    try:
        if failure == "enumeration":

            def fail(host):
                raise OSError("interface temporarily unavailable")

            monkeypatch.setattr(bonjour, "_bound_addresses", fail)
        else:
            registrations[0][1].async_close.side_effect = OSError("old interface disappeared")

            def fail_registration(**kwargs):
                zeroconf = AsyncMock()
                zeroconf.async_register_service.side_effect = OSError("network not ready")
                registrations.append((kwargs, zeroconf))
                return zeroconf

            monkeypatch.setattr(bonjour, "AsyncZeroconf", fail_registration)
            network = (["192.168.3.90"], ["192.168.3.90"])
        await publisher._refresh("0.0.0.0")
        assert publisher._zeroconf is None
        if failure == "registration":
            registrations[-1][1].async_close.assert_awaited_once()
        recovered = AsyncMock()
        announcements = AsyncMock()
        recovered.async_register_service.side_effect = lambda *a, **kw: announcements()
        monkeypatch.setattr(bonjour, "AsyncZeroconf", lambda **kw: recovered)
        monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: network)
        await publisher._refresh("0.0.0.0")
        recovered.async_register_service.assert_awaited_once()
    finally:
        await publisher.close()


async def test_watcher_refreshes_and_stops_on_close(monkeypatch, registrations):
    monkeypatch.setattr(bonjour, "_NETWORK_CHECK_INTERVAL", 0.001)
    network = (["192.168.0.21"], ["192.168.0.21"])
    monkeypatch.setattr(bonjour, "_bound_addresses", lambda host: network)
    publisher = bonjour.BonjourPublisher()
    await publisher.start(host="0.0.0.0", port=8088, tls=False)
    task = publisher._watch_task
    network = (["192.168.3.90"], ["192.168.3.90"])
    try:
        async with asyncio.timeout(2):
            while len(registrations) < 2 or publisher._network is None:
                await asyncio.sleep(0.001)
        info = registrations[1][1].async_register_service.call_args.args[0]
        assert info.parsed_addresses() == ["192.168.3.90"]
    finally:
        await publisher.close()
    assert task.done()
    assert publisher._zeroconf is None
