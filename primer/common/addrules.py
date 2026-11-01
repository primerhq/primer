"""The ONE address rule of the platform's SSRF guards (review of gaps-security, GS-44).

:mod:`primer.common.netguard` (connect-time guard of the agent web tools and the MCP client, with the operator's CIDR allowlist) and
:mod:`primer.common.ssrf` (workspace url sources and the OAuth endpoints an MCP server names, with the url-source opt-in) used to carry their own
idea of a public address, and each let loopback through an IPv6 form the other refused. Both now ask this module.

An IPv6 address that carries an IPv4 one is judged by that IPv4 address, whichever way it is written:

* IPv4-mapped ``::ffff:a.b.c.d``;
* the NAT64 well-known prefix ``64:ff9b::/96`` (a gateway translates it to the embedded address);
* 6to4 ``2002:a.b.c.d::/48``;
* the deprecated IPv4-compatible ``::a.b.c.d`` (``::`` and ``::1`` are the unspecified and loopback addresses themselves);
* SIIT, the IPv4-translated ``::ffff:0:a.b.c.d``.

Python reports the compatible and SIIT forms as ``is_global``, so a rule that only asked ``is_global`` let ``::7f00:1`` through.
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_IPV4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")

#: Cloud metadata endpoints that are not (all) link-local: refused even when an operator turns the private-destination refusal off (ruling D19).
_METADATA = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254")
)


def embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address ``ip`` carries (any of the forms in the module docstring), else ``None``."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64 or ip in _IPV4_TRANSLATED:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _IPV4_COMPATIBLE and int(ip) > 1:  # :: and ::1 are themselves
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def unwrap(ip: IPAddress) -> IPAddress:
    """``ip``, or the IPv4 address it carries when it is an IPv6 form of one."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = embedded_v4(ip)
        if inner is not None:
            return inner
    return ip


def parse(address: str) -> IPAddress | None:
    """``address`` as an IP address (a zone id and brackets are dropped), else ``None``."""
    try:
        return ipaddress.ip_address(address.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def refusal_reason(ip: IPAddress) -> str | None:
    """Why ``ip`` is not a public unicast address, or ``None`` when it is."""
    ip = unwrap(ip)
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if is_link_local_or_metadata(ip):
        return "link-local (includes the cloud metadata address)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private or not ip.is_global:
        return "private or reserved"
    return None


def is_public(ip: IPAddress) -> bool:
    """Whether ``ip`` (unwrapped) is a public unicast address."""
    return refusal_reason(ip) is None


def is_link_local_or_metadata(ip: IPAddress) -> bool:
    """Whether ``ip`` (unwrapped) is link-local (``169.254.0.0/16``, ``fe80::/10``) or a cloud metadata address: the destinations no opt-in
    opens (ruling D19)."""
    ip = unwrap(ip)
    return ip.is_link_local or ip in _METADATA


__all__ = ["embedded_v4", "is_link_local_or_metadata", "is_public", "parse", "refusal_reason", "unwrap"]
