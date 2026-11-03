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
    await bonjour.BonjourPublisher().start(host="0.0.0.0", port=8088, tls=False)


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
