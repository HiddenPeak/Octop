"""Best-effort LAN discovery for the foreground HTTP service."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from uuid import UUID, uuid4

import ifaddr
from zeroconf import DNSQuestionType, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from inhere_box import __version__

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_inhere-box._tcp.local."
_NETWORK_CHECK_INTERVAL = 5.0
_PROBE_INTERVAL = 30.0
_PROBE_TIMEOUT_MS = 2000
_PROBE_FAILURE_LIMIT = 3


def _bound_addresses(host: str) -> tuple[list[str], list[str | int]]:
    """Enumerate only local interface addresses served by the bind host.

    Uvicorn's IPv6 wildcard socket is IPv6-only. IPv6 interface indexes retain
    scope for multicast, while DNS AAAA records contain the unscoped address.
    Run off the event loop: both DNS resolution and adapter enumeration block.
    """
    bound = {
        ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
        for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    }
    wildcard_versions = {address.version for address in bound if address.is_unspecified}
    addresses: list[str] = []
    interfaces: list[str | int] = []
    for adapter in ifaddr.get_adapters():
        for entry in adapter.ips:
            raw = entry.ip if isinstance(entry.ip, str) else entry.ip[0]
            address = ipaddress.ip_address(raw.split("%", 1)[0])
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                continue
            if address.version not in wildcard_versions and address not in bound:
                continue
            interface: str | int = (
                adapter.index if address.version == 6 and adapter.index is not None else raw
            )
            if str(address) not in addresses:
                addresses.append(str(address))
            if interface not in interfaces:
                interfaces.append(interface)
    return addresses, interfaces


async def _advertisement_healthy(
    info: ServiceInfo, interfaces: list[str | int], ip_version: IPVersion
) -> bool:
    # A fresh resolver must receive the reply over multicast. The publisher's
    # own registration and a unicast reply can both outlive its multicast path.
    async with AsyncZeroconf(interfaces=interfaces, ip_version=ip_version) as probe:
        reply = await probe.async_get_service_info(
            info.type, info.name, timeout=_PROBE_TIMEOUT_MS, question_type=DNSQuestionType.QM
        )
        return (
            reply is not None
            and reply.server == info.server
            and reply.port == info.port
            and not set(info.parsed_addresses()).isdisjoint(reply.parsed_addresses())
        )


class BonjourPublisher:
    """Own a single registration, without changing HTTP binding or auth."""

    def __init__(self) -> None:
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._network: tuple[frozenset[str], frozenset[str | int]] | None = None
        self._probe_at = 0.0
        self._probe_failures = 0
        self._watch_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self, *, host: str, port: int, tls: bool, box_id: str | None = None) -> None:
        await self.close()
        try:
            # Keep both DNS labels below 63 bytes, including on long/non-ASCII hosts.
            hostname = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())
            hostname = hostname[:40].strip("-") or "host"
            # A configured box's DNS name must survive process restarts: clients
            # save its SRV target. The installation ID also separates boxes that
            # share a factory hostname. Pre-setup discovery has no saved login yet.
            label = (
                f"inhere-box-{UUID(box_id).hex}"
                if box_id is not None
                else f"inhere-box-{hostname}-{uuid4().hex[:8]}"
            )
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"inhere-box {hostname} ({port}).{SERVICE_TYPE}",
                server=f"{label}.local.",
                port=port,
                parsed_addresses=[],
                properties={
                    "txtvers": "1",
                    "product": "inhere-box",
                    "version": __version__,
                    "scheme": "https" if tls else "http",
                    "path": "/",
                },
            )
        except Exception:
            logger.warning(
                "Bonjour initialization failed; HTTP service remains available", exc_info=True
            )
            return
        await self._refresh(host)
        self._stop.clear()
        self._watch_task = asyncio.create_task(self._watch(host), name="bonjour-network-watch")

    async def _watch(self, host: str) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_NETWORK_CHECK_INTERVAL)
            except TimeoutError:
                await self._refresh(host)

    async def _refresh(self, host: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            addresses, interfaces = await loop.run_in_executor(None, _bound_addresses, host)
            network = (frozenset(addresses), frozenset(interfaces))
            versions = {ipaddress.ip_address(address).version for address in addresses}
            ip_version = (
                IPVersion.All
                if len(versions) > 1
                else IPVersion.V6Only
                if versions == {6}
                else IPVersion.V4Only
            )
            if self._zeroconf is not None and network == self._network:
                if loop.time() < self._probe_at:
                    return
                info = self._info
                assert info is not None
                try:
                    healthy = await _advertisement_healthy(info, interfaces, ip_version)
                except Exception:
                    logger.debug("Bonjour multicast probe failed", exc_info=True)
                    healthy = False
                if healthy:
                    self._probe_at = loop.time() + _PROBE_INTERVAL
                    self._probe_failures = 0
                    return
                self._probe_failures += 1
                if self._probe_failures < _PROBE_FAILURE_LIMIT:
                    return
                logger.warning("Bonjour multicast replies unavailable; rebuilding registration")
            # Sockets are bound to the old interfaces. Rebuild them as well as
            # the A/AAAA records to recover from address or multicast changes.
            await self._close_registration()
            if not addresses:
                return
            previous = self._info
            assert previous is not None
            info = ServiceInfo(
                previous.type,
                previous.name,
                server=previous.server,
                port=previous.port,
                parsed_addresses=addresses,
                properties=previous.properties,
            )
            self._zeroconf = AsyncZeroconf(interfaces=interfaces, ip_version=ip_version)
            announcements = await self._zeroconf.async_register_service(
                info, allow_name_change=True
            )
            await announcements
            self._info = info
            self._network = network
            self._probe_at = loop.time() + _PROBE_INTERVAL
            self._probe_failures = 0
            logger.info("Bonjour registered %s on port %s at %s", info.name, info.port, addresses)
        except Exception:
            logger.warning(
                "Bonjour registration failed; HTTP service remains available", exc_info=True
            )
            await self._close_registration()

    async def close(self) -> None:
        self._stop.set()
        task, self._watch_task = self._watch_task, None
        if task is not None:
            await task
        await self._close_registration()

    async def _close_registration(self) -> None:
        self._network = None
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is not None:
            try:
                # Sends goodbye records and releases every multicast socket.
                await zeroconf.async_close()
            except Exception:
                logger.warning("Bonjour cleanup failed", exc_info=True)
