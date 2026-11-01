"""One address rule for both SSRF modules (review of gaps-security, GS-44; lead ruling D19).

``primer.common.netguard`` (agent web tools, MCP save and connect, the egress allowlist) and ``primer.common.ssrf`` (workspace url sources and the
MCP OAuth endpoints) each carried their own idea of "a public address": netguard did not unwrap the IPv4-compatible (``::7f00:1``) and SIIT
(``::ffff:0:7f00:1``) forms, ssrf did not unwrap 6to4 (``2002:7f00:1::``), so each let loopback through a form the other refused. They now share
one helper (:mod:`primer.common.addrules`): every table row below gets the same verdict from both. Under D19 the url-source opt-in
(``workspace_allow_private_url_sources``) lets private and loopback addresses through but never link-local or cloud-metadata ones.
"""

from __future__ import annotations

import ipaddress

import pytest

from primer.common import netguard, ssrf

#: (address, public?) - the verdict BOTH modules must give with no opt-in.
TABLE = [
    # plain v4
    ("8.8.8.8", True), ("93.184.216.34", True), ("1.1.1.1", True),
    ("127.0.0.1", False), ("127.1.2.3", False), ("0.0.0.0", False), ("10.0.0.5", False), ("172.16.0.9", False), ("192.168.1.10", False),
    ("169.254.169.254", False), ("169.254.1.1", False), ("100.64.0.1", False), ("224.0.0.1", False), ("255.255.255.255", False),
    ("192.0.2.1", False), ("198.51.100.1", False), ("203.0.113.1", False),
    # plain v6
    ("2606:4700:4700::1111", True), ("2001:4860:4860::8888", True),
    ("::1", False), ("::", False), ("fe80::1", False), ("fd00::5", False), ("fc00::1", False), ("ff02::1", False), ("2001:db8::1", False),
    # IPv4-mapped
    ("::ffff:8.8.8.8", True), ("::ffff:127.0.0.1", False), ("::ffff:10.1.2.3", False), ("::ffff:169.254.169.254", False),
    # NAT64 well-known prefix
    ("64:ff9b::808:808", True), ("64:ff9b::7f00:1", False), ("64:ff9b::a00:5", False), ("64:ff9b::a9fe:a9fe", False),
    # 6to4 (2002::/16 carries the IPv4 address in the next 32 bits)
    ("2002:808:808::1", True), ("2002:7f00:1::1", False), ("2002:a00:5::1", False), ("2002:a9fe:a9fe::1", False),
    # IPv4-compatible (deprecated ::a.b.c.d)
    ("::808:808", True), ("::7f00:1", False), ("::a00:5", False), ("::a9fe:a9fe", False),
    # SIIT (::ffff:0:a.b.c.d)
    ("::ffff:0:808:808", True), ("::ffff:0:7f00:1", False), ("::ffff:0:a00:5", False), ("::ffff:0:a9fe:a9fe", False),
]


@pytest.fixture(autouse=True)
def _no_opt_ins():
    netguard.configure_egress_allow([])
    ssrf.configure_allow_private_destinations(False)
    yield
    netguard.configure_egress_allow([])
    ssrf.configure_allow_private_destinations(False)


def _netguard_public(address: str) -> bool:
    try:
        netguard.vet_ip_literal(address)
    except netguard.EgressRefused:
        return False
    return True


def _ssrf_public(address: str) -> bool:
    return ssrf.blocked_reason(address) is None


@pytest.mark.parametrize(("address", "public"), TABLE)
def test_netguard_gives_every_row_its_verdict(address, public):
    assert _netguard_public(address) is public, address


@pytest.mark.parametrize(("address", "public"), TABLE)
def test_ssrf_gives_every_row_its_verdict(address, public):
    assert _ssrf_public(address) is public, address


@pytest.mark.parametrize(("address", "_public"), TABLE)
def test_the_two_modules_agree_on_every_row(address, _public):
    assert _netguard_public(address) == _ssrf_public(address), address


def test_the_table_covers_every_embedded_form_it_names():
    kinds = {"mapped": 0, "nat64": 0, "6to4": 0, "compat": 0, "siit": 0}
    for address, _ in TABLE:
        ip = ipaddress.ip_address(address)
        if ip.version != 6:
            continue
        if ip.ipv4_mapped is not None:
            kinds["mapped"] += 1
        elif ip in ipaddress.IPv6Network("64:ff9b::/96"):
            kinds["nat64"] += 1
        elif ip in ipaddress.IPv6Network("2002::/16"):
            kinds["6to4"] += 1
        elif ip in ipaddress.IPv6Network("::ffff:0:0:0/96"):
            kinds["siit"] += 1
        elif ip in ipaddress.IPv6Network("::/96") and int(ip) > 1:
            kinds["compat"] += 1
    assert all(count >= 4 for count in kinds.values()), kinds


# ---- the reasons ssrf logs stay what they were ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("address, reason", [
    ("0.0.0.0", "unspecified"), ("127.0.0.1", "loopback"), ("::1", "loopback"), ("169.254.169.254", "link-local (includes the cloud metadata address)"),
    ("fe80::1", "link-local (includes the cloud metadata address)"), ("224.0.0.1", "multicast"), ("10.0.0.5", "private or reserved"),
    ("::ffff:127.0.0.1", "loopback"), ("::7f00:1", "loopback"), ("2002:7f00:1::1", "loopback"), ("not-an-ip", "not an IP address"),
])
def test_ssrf_reasons(address, reason):
    assert ssrf.blocked_reason(address) == reason


# ---- the operator allowlist (netguard) still matches embedded forms of an allowed v4 --------------------------------------------------------


def test_the_egress_allowlist_is_honoured_for_an_address_and_its_embedded_forms():
    netguard.configure_egress_allow(["10.0.0.0/8"])

    assert _netguard_public("10.0.0.5") and _netguard_public("::ffff:10.0.0.5") and _netguard_public("::a00:5") and _netguard_public("::ffff:0:a00:5")
    assert not _netguard_public("192.168.0.1") and not _netguard_public("::c0a8:1")


# ---- D19: the url-source opt-in never reaches link-local or metadata -------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "192.168.1.1", "::1", "fd00::5", "100.64.0.1", "::ffff:10.1.2.3"])
def test_the_url_source_opt_in_still_lets_private_and_loopback_through(address):
    ssrf.configure_allow_private_destinations(True)

    assert ssrf.blocked_reason(address) is not None, "the address itself is still not public"
    ssrf.refuse_private_literal(f"http://[{address}]/x" if ":" in address else f"http://{address}/x")  # does not raise


@pytest.mark.parametrize("address", [
    "169.254.169.254", "169.254.1.1", "fe80::1", "fe80::a9fe:a9fe", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", "::a9fe:a9fe",
    "::ffff:0:a9fe:a9fe", "fd00:ec2::254", "100.100.100.200",
])
def test_the_url_source_opt_in_never_reaches_link_local_or_metadata(address):
    ssrf.configure_allow_private_destinations(True)
    url = f"http://[{address}]/x" if ":" in address else f"http://{address}/x"

    with pytest.raises(ssrf.BlockedDestinationError):
        ssrf.refuse_private_literal(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["169.254.169.254", "fe80::1", "::ffff:169.254.169.254", "fd00:ec2::254"])
async def test_mcp_oauth_refuses_metadata_with_the_url_source_opt_in_on(address):
    ssrf.configure_allow_private_destinations(True)

    with pytest.raises(ssrf.BlockedDestinationError):
        await ssrf.public_addresses(address, 80)


@pytest.mark.asyncio
async def test_mcp_oauth_with_the_opt_in_on_still_reaches_a_private_name(monkeypatch):
    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf, "_resolve", private)
    ssrf.configure_allow_private_destinations(True)

    assert await ssrf.public_addresses("auth.corp.example", 443) == ["10.0.0.5"]


@pytest.mark.asyncio
async def test_a_name_with_one_metadata_answer_is_refused_even_with_the_opt_in(monkeypatch):
    async def mixed(host: str, port: int) -> list[str]:
        return ["10.0.0.5", "169.254.169.254"]

    monkeypatch.setattr(ssrf, "_resolve", mixed)
    ssrf.configure_allow_private_destinations(True)

    with pytest.raises(ssrf.BlockedDestinationError):
        await ssrf.public_addresses("auth.corp.example", 443)


@pytest.mark.asyncio
async def test_the_resolver_for_url_sources_refuses_metadata_with_the_opt_in_on():
    ssrf.configure_allow_private_destinations(True)

    class _Inner:
        async def resolve(self, host, port=0, family=0):
            return [{"hostname": host, "host": "169.254.169.254", "port": port, "family": 2, "proto": 0, "flags": 0}]

        async def close(self):
            return None

    with pytest.raises(ssrf.BlockedDestinationError):
        await ssrf.PublicOnlyResolver(_Inner()).resolve("files.corp.example", 80)
