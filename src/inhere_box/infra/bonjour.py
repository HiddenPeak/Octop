"""Best-effort LAN discovery for the foreground HTTP service."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from uuid import UUID, uuid4

import ifaddr
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from inhere_box import __version__

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_inhere-box._tcp.local."


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


class BonjourPublisher:
    """Own a single registration, without changing HTTP binding or auth."""

    def __init__(self) -> None:
        self._zeroconf: AsyncZeroconf | None = None

    async def start(self, *, host: str, port: int, tls: bool, box_id: str | None = None) -> None:
        try:
            addresses, interfaces = await asyncio.to_thread(_bound_addresses, host)
            if not addresses:
                logger.info("Bonjour skipped: no LAN addresses for bind host %s", host)
                return
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
            info = ServiceInfo(
                SERVICE_TYPE,
                f"inhere-box {hostname} ({port}).{SERVICE_TYPE}",
                server=f"{label}.local.",
                port=port,
                parsed_addresses=addresses,
                properties={
                    "txtvers": "1",
                    "product": "inhere-box",
                    "version": __version__,
                    "scheme": "https" if tls else "http",
                    "path": "/",
                },
            )
            versions = {ipaddress.ip_address(address).version for address in addresses}
            ip_version = (
                IPVersion.All
                if len(versions) > 1
                else IPVersion.V4Only
                if 4 in versions
                else IPVersion.V6Only
            )
            self._zeroconf = AsyncZeroconf(interfaces=interfaces, ip_version=ip_version)
            announcements = await self._zeroconf.async_register_service(
                info, allow_name_change=True
            )
            await announcements
            logger.info("Bonjour registered %s on port %s", info.name, port)
        except Exception:
            logger.warning(
                "Bonjour registration failed; HTTP service remains available", exc_info=True
            )
            await self.close()

    async def close(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        if zeroconf is not None:
            try:
                # Sends goodbye records and releases every multicast socket.
                await zeroconf.async_close()
            except Exception:
                logger.warning("Bonjour cleanup failed", exc_info=True)
