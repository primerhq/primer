"""Refuse outbound fetches to private, loopback, link-local and metadata addresses (security review 2026-10-08, AUTHZ-05).

A minimal resolve-then-check guard for the platform's own fetches of a user-supplied URL (today: a workspace template's
``kind=url`` file source). Two pieces:

* :func:`refuse_private_literal` refuses a URL whose host is an IP literal in a blocked range, before any connection;
* :class:`PublicOnlyResolver` is an ``aiohttp`` resolver that refuses a host name when ANY address it resolves to is in a
  blocked range. The connector connects to the addresses this resolver returned, so a name re-bound between a check and
  the connect cannot slip through.

aiohttp skips the resolver for an IP literal, so a caller that follows redirects must run :func:`refuse_private_literal`
on every hop. The parallel SSRF change (branch ``fix/sec-ssrf-guard``) adds a shared egress guard; when it lands this
module may be folded into it.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL


class BlockedDestinationError(ValueError):
    """The destination is an address the platform must not fetch from."""


def blocked_reason(address: str) -> str | None:
    """Why ``address`` (an IP literal) is refused, or ``None`` when it is a public unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "not an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
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
    host = URL(url).host or ""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return  # a host name: the resolver checks it at connect time
    reason = blocked_reason(host)
    if reason is not None:
        raise BlockedDestinationError(f"destination {host} is {reason}")


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
            reason = blocked_reason(r["host"])
            if reason is not None:
                raise BlockedDestinationError(f"destination {host} resolves to {r['host']}, which is {reason}")
        return results

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


__all__ = ["BlockedDestinationError", "PublicOnlyResolver", "blocked_reason", "refuse_private_literal"]
