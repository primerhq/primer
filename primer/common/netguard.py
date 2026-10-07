"""Outbound-request guard: refuse internal addresses (SSRF-03).

Agent tools (``http_request``, ``web_fetch``'s local adapter, ``download``)
fetch URLs chosen by a user or by an agent that may be prompt-injected. Without a guard those requests reach whatever the
platform process can reach: cloud metadata (169.254.169.254), the
Kubernetes API, the Postgres host, localhost admin ports.

The rule, applied to every connection (so to every redirect hop too):

* resolve the host to ALL its A/AAAA records;
* refuse when ANY record is not a public unicast address (loopback,
  RFC1918, link-local incl. metadata, CGNAT, ULA, multicast, unspecified,
  reserved, and the IPv4-mapped / NAT64 / 6to4 forms of those);
* connect to the vetted address itself, so a second DNS answer (DNS
  rebinding) cannot swap in an internal one. The Host header and the TLS
  SNI / certificate check still use the original name.

An operator opts internal targets back in with ``egress_allow`` in
:class:`~primer.api.config.AppConfig` (env ``PRIMER_EGRESS_ALLOW``, a JSON
list of CIDRs, IPs or exact host names). It is empty by default. The
lifespan hands it to :func:`configure_egress_allow` once at boot; the
policy is process-wide because the guarded clients are built deep inside
factories that never see the config.

Public surface:

* :func:`vet_host` - resolve and check, return the addresses to connect to.
* :func:`guarded_async_client` - an :class:`httpx.AsyncClient` whose every
  connection goes through :func:`vet_host`.
* :class:`GuardedResolver` - the same rule as an aiohttp resolver (for the
  workspace url file mounts, which still use aiohttp; not wired yet).
* :func:`vet_ip_literal` - the literal-address check aiohttp needs (it
  bypasses its resolver for IP literals).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from aiohttp.abc import AbstractResolver, ResolveResult


ALLOW_CONFIG_KEY = "PRIMER_EGRESS_ALLOW"

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?)(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?)*$"
)


class EgressRefused(httpx.RequestError):
    """An outbound request was refused because its target is internal.

    A subclass of :class:`httpx.RequestError` so every existing
    ``except httpx.RequestError`` handler turns it into a tool error rather
    than a crash.
    """


# ---- operator allowlist ------------------------------------------------------


_allow_networks: tuple[_IPNetwork, ...] = ()
_allow_hosts: frozenset[str] = frozenset()


def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def parse_egress_allow(
    entries: Iterable[str],
) -> tuple[tuple[_IPNetwork, ...], frozenset[str]]:
    """Split allowlist entries into networks and host names.

    An entry is a CIDR (``10.0.0.0/8``), a single address (``127.0.0.1``)
    or an exact host name (``registry.internal``). Raises ``ValueError``
    for anything else, so a typo fails at boot instead of silently
    allowing nothing.
    """
    nets: list[_IPNetwork] = []
    hosts: set[str] = set()
    for raw in entries:
        entry = raw.strip()
        if not entry:
            raise ValueError("egress_allow: empty entry")
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        host = _norm_host(entry)
        if "/" in entry or not _HOSTNAME_RE.match(host):
            raise ValueError(
                f"egress_allow: {raw!r} is not a CIDR, an IP address or a host name"
            )
        hosts.add(host)
    return tuple(nets), frozenset(hosts)


def configure_egress_allow(entries: Iterable[str]) -> None:
    """Install the process-wide allowlist (called once by the lifespan)."""
    global _allow_networks, _allow_hosts
    _allow_networks, _allow_hosts = parse_egress_allow(entries)


# ---- the address rule --------------------------------------------------------


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    return None


def _is_public(ip: _IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            return _is_public(inner)
    return ip.is_global and not ip.is_multicast


def _is_allowed(ip: _IPAddress) -> bool:
    if _is_public(ip):
        return True
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)
    return any(c in net for c in candidates for net in _allow_networks if c.version == net.version)


def _refusal(host: str, ip: str) -> EgressRefused:
    return EgressRefused(
        f"refused: {host} resolves to a private address ({ip}); an operator "
        f"can allow it with {ALLOW_CONFIG_KEY}"
    )


def _parse_ip(host: str) -> _IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (tests monkeypatch this)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0]).split("%", 1)[0]
        if addr not in out:
            out.append(addr)
    return out


def vet_ip_literal(host: str) -> None:
    """Refuse ``host`` when it is an internal IP literal; names pass."""
    ip = _parse_ip(host)
    if ip is not None and not _is_allowed(ip):
        raise _refusal(host, str(ip))


async def vet_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` and return the addresses it is safe to connect to.

    Raises :class:`EgressRefused` when any address is internal and not on
    the operator allowlist (one bad record refuses the whole name: the
    client could otherwise be steered to it). An allowlisted host name
    skips the address check.
    """
    literal = _parse_ip(host)
    if literal is not None:
        vet_ip_literal(host)
        return [str(literal)]
    try:
        addrs = await _resolve(host, port)
    except OSError as exc:
        raise httpx.ConnectError(f"cannot resolve {host}: {exc}") from exc
    if not addrs:
        raise httpx.ConnectError(f"cannot resolve {host}: no addresses")
    if _norm_host(host) in _allow_hosts:
        return addrs
    for addr in addrs:
        ip = _parse_ip(addr)
        if ip is None or not _is_allowed(ip):
            raise _refusal(host, addr)
    return addrs


# ---- httpx -------------------------------------------------------------------


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that vets, then connects to the vetted IP.

    httpcore calls ``connect_tcp`` with the URL's host for every new
    connection (the first request and each redirect hop to a new origin);
    a reused pooled connection was vetted when it was opened. The TLS
    handshake that follows still gets the original name as SNI, so the
    certificate is checked against the name, not the IP.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addrs = await vet_host(host, port)
        last: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    addr, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise EgressRefused("refused: unix sockets are not an allowed egress target")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_async_client(
    *,
    network_backend: httpcore.AsyncNetworkBackend | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """An :class:`httpx.AsyncClient` whose connections all pass :func:`vet_host`.

    ``network_backend`` is the backend the vetted connection is opened on
    (tests pass an httpcore mock). An explicit transport also means httpx
    ignores the ``HTTP(S)_PROXY`` environment, so the guard always sees
    the real destination.
    """
    transport = httpx.AsyncHTTPTransport()
    # httpx exposes no network_backend knob; swap in a pool built with one.
    # The pool keeps httpx's defaults (verified TLS, HTTP/1.1, default limits).
    transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001
        ssl_context=httpx.create_ssl_context(),
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=5.0,
        network_backend=GuardedNetworkBackend(network_backend),
    )
    return httpx.AsyncClient(transport=transport, **client_kwargs)


# ---- aiohttp -----------------------------------------------------------------


class GuardedResolver(AbstractResolver):
    """aiohttp resolver applying :func:`vet_host`; aiohttp connects to its answer.

    aiohttp does not call the resolver for an IP-literal URL, so callers
    must also run :func:`vet_ip_literal` on each hop's host.
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        addrs = await vet_host(host, port)
        return [
            ResolveResult(
                hostname=host,
                host=addr,
                port=port,
                family=socket.AF_INET6 if ":" in addr else socket.AF_INET,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
            for addr in addrs
        ]

    async def close(self) -> None:
        return None


__all__ = [
    "ALLOW_CONFIG_KEY",
    "EgressRefused",
    "GuardedNetworkBackend",
    "GuardedResolver",
    "configure_egress_allow",
    "guarded_async_client",
    "parse_egress_allow",
    "vet_host",
    "vet_ip_literal",
]
