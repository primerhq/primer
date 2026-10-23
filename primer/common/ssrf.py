"""Refuse outbound fetches to private, loopback, link-local and metadata addresses (security review 2026-10-08, AUTHZ-05).

A minimal resolve-then-check guard for the platform's own fetches of a user-supplied URL (a workspace template's
``kind=url`` file source, and the OAuth endpoints an MCP server names on another host than its own: discovery documents and
the token, registration and revocation URLs, audit G4-08). Three pieces:

* :func:`refuse_private_literal` refuses a URL whose host is an IP literal in a blocked range, before any connection;
* :class:`PublicOnlyResolver` is an ``aiohttp`` resolver that refuses a host name when ANY address it resolves to is in a
  blocked range. The connector connects to the addresses this resolver returned, so a name re-bound between a check and
  the connect cannot slip through.
* :func:`public_addresses` is the same rule for an httpx connection: the MCP OAuth client
  (:mod:`primer.toolset.oauth.egress`) vets each new connection with it through
  :class:`primer.common.netguard.GuardedNetworkBackend`, which connects to the address that passed.

aiohttp skips the resolver for an IP literal, so a caller that follows redirects must run :func:`refuse_private_literal`
on every hop. The parallel SSRF change (branch ``fix/sec-ssrf-guard``) adds a shared egress guard; when it lands this
module may be folded into it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

_log = logging.getLogger(__name__)


_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_IPV4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")


# Operator opt-in (AppConfig.workspace_allow_private_url_sources, env PRIMER_WORKSPACE_ALLOW_PRIVATE_URL_SOURCES):
# a deployment that seeds workspaces from an internal file server, or whose MCP servers name an authorization server on a
# private address, turns the refusal off. Set once at boot.
_ALLOW_PRIVATE = False


def configure_allow_private_destinations(enabled: bool) -> None:
    """Turn the private-destination refusal off (``True``) or back on (``False``) for this process."""
    global _ALLOW_PRIVATE
    _ALLOW_PRIVATE = bool(enabled)


class BlockedDestinationError(ValueError):
    """The destination is an address the platform must not fetch from."""


def blocked_reason(address: str) -> str | None:
    """Why ``address`` (an IP literal) is refused, or ``None`` when it is a public unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv6 address that carries an IPv4 one is judged by that IPv4: IPv4-mapped (::ffff:a.b.c.d), the NAT64
        # well-known prefix (64:ff9b::/96, which a NAT64 gateway translates to the embedded address) and the
        # deprecated IPv4-compatible form (::a.b.c.d; :: and ::1 unwrap to 0.0.0.0 and 0.0.0.1, both refused), and the
        # SIIT IPv4-translated form (::ffff:0:a.b.c.d).
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_WELL_KNOWN or ip in _IPV4_COMPATIBLE or ip in _IPV4_TRANSLATED:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (includes the cloud metadata address)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private or not ip.is_global:
        return "private or reserved"
    return None


def refuse_private_literal(url: str) -> None:
    """Raise :class:`BlockedDestinationError` when ``url``'s host is an IP literal in a blocked range."""
    if _ALLOW_PRIVATE:
        return
    host = URL(url).host or ""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return  # a host name: the resolver checks it at connect time
    reason = blocked_reason(host)
    if reason is not None:
        _log.warning("url source refused: %s is %s", host, reason)
        raise BlockedDestinationError(f"destination {host} is not a public address")


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, in the resolver's order (tests replace this)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


async def public_addresses(host: str, port: int) -> list[str]:
    """The addresses to connect to for ``host``: refused (:class:`BlockedDestinationError`) when ANY address it resolves to, or the IP
    literal it is, is in a blocked range, as :class:`PublicOnlyResolver` does for aiohttp. The operator's opt-in returns them unchecked.
    A name that does not resolve raises ``OSError``."""
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal.split("%", 1)[0])
    except ValueError:
        addresses = await _resolve(host, port)
    else:
        addresses = [literal]
    if not addresses:
        raise OSError(f"{host} resolves to no address")
    if _ALLOW_PRIVATE:
        return addresses
    for address in addresses:
        reason = blocked_reason(address)
        if reason is not None:
            # As for the url sources: the address and the reason go to the server log only, the error names the host.
            _log.warning("destination refused: %s resolves to %s, which is %s", host, address, reason)
            raise BlockedDestinationError(f"destination {host} is not a public address")
    return addresses


class PublicOnlyResolver(AbstractResolver):
    """An aiohttp resolver that refuses a name resolving to any blocked address."""

    def __init__(self, inner: Any | None = None) -> None:
        self._inner = inner

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if self._inner is None:
            from aiohttp.resolver import DefaultResolver
            self._inner = DefaultResolver()
        results = await self._inner.resolve(host, port, family=family)
        if _ALLOW_PRIVATE:
            return results
        for r in results:
            reason = blocked_reason(r["host"])
            if reason is not None:
                # The resolved address goes to the server log only: the error reaches the template author (a 422
                # detail), and echoing it would make the platform an oracle for internal DNS.
                _log.warning("url source refused: %s resolves to %s, which is %s", host, r["host"], reason)
                raise BlockedDestinationError(f"destination {host} is not a public address")
        return results

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


__all__ = [
    "BlockedDestinationError",
    "PublicOnlyResolver",
    "blocked_reason",
    "configure_allow_private_destinations",
    "public_addresses",
    "refuse_private_literal",
]
