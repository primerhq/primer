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

from primer.common import addrules

_log = logging.getLogger(__name__)


# Operator opt-in (AppConfig.workspace_allow_private_url_sources, env PRIMER_WORKSPACE_ALLOW_PRIVATE_URL_SOURCES):
# a deployment that seeds workspaces from an internal file server, or whose MCP servers name an authorization server on a
# private address, lets PRIVATE and loopback destinations through. It never opens link-local or cloud-metadata ones (169.254.0.0/16,
# fe80::/10, 169.254.169.254, fd00:ec2::254, ...; lead ruling D19, review GS-44): an internal file server is not on link-local, and a hostile
# MCP server's OAuth endpoints must not reach the metadata service because an operator wanted a file server. Set once at boot.
_ALLOW_PRIVATE = False


def configure_allow_private_destinations(enabled: bool) -> None:
    """Turn the private-destination refusal off (``True``) or back on (``False``) for this process."""
    global _ALLOW_PRIVATE
    _ALLOW_PRIVATE = bool(enabled)


class BlockedDestinationError(ValueError):
    """The destination is an address the platform must not fetch from."""


def blocked_reason(address: str) -> str | None:
    """Why ``address`` (an IP literal) is not a public unicast address, or ``None`` when it is: the platform's one address rule
    (:mod:`primer.common.addrules`, shared with :mod:`primer.common.netguard`). Independent of the opt-in; see :func:`_refused`."""
    ip = addrules.parse(address)
    if ip is None:
        return "not an IP address"
    return addrules.refusal_reason(ip)


def _refused(address: str) -> str | None:
    """Why a connection to ``address`` is refused NOW: :func:`blocked_reason`, except that with the operator's opt-in on only link-local and
    cloud-metadata addresses are still refused (ruling D19)."""
    reason = blocked_reason(address)
    if reason is None or reason == "not an IP address":
        return reason
    if _ALLOW_PRIVATE:
        ip = addrules.parse(address)
        if ip is not None and not addrules.is_link_local_or_metadata(ip):
            return None
    return reason


def refuse_private_literal(url: str) -> None:
    """Raise :class:`BlockedDestinationError` when ``url``'s host is an IP literal in a blocked range (private ones too unless the opt-in is on;
    link-local and metadata always)."""
    host = URL(url).host or ""
    if addrules.parse(host) is None:
        return  # a host name: the resolver checks it at connect time
    reason = _refused(host)
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
    literal it is, is in a blocked range, as :class:`PublicOnlyResolver` does for aiohttp. The operator's opt-in lets private and loopback
    addresses through, never link-local or metadata ones (ruling D19). A name that does not resolve raises ``OSError``."""
    literal = host.strip("[]")
    if addrules.parse(literal) is None:
        addresses = await _resolve(host, port)
    else:
        addresses = [literal]
    if not addresses:
        raise OSError(f"{host} resolves to no address")
    for address in addresses:
        reason = _refused(address)
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
        for r in results:
            reason = _refused(r["host"])
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
