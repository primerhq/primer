"""Unit tests for primer.common.netguard (SSRF-03 / SSRF-04 egress guard).

No real network: the resolver is monkeypatched and the httpx client runs
over a recording httpcore mock network backend, so the tests see exactly
which address a connection was opened to and which bytes were written.
"""

from __future__ import annotations

import time

import anyio
import httpcore
import httpx
import pytest

from primer.common import netguard
from primer.common.netguard import EgressRefused


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@pytest.fixture(autouse=True)
def _reset_allowlist():
    netguard.configure_egress_allow([])
    yield
    netguard.configure_egress_allow([])


def _fake_dns(monkeypatch, table: dict[str, list[str]]) -> list[str]:
    """Route netguard's resolver through ``table``; return the lookup log."""
    calls: list[str] = []

    async def _resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        if host in table:
            return list(table[host])
        # An IP literal resolves to itself, as getaddrinfo does.
        return [host]

    monkeypatch.setattr(netguard, "_resolve", _resolve)
    return calls


class _RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer, log):
        super().__init__(buffer)
        self._log = log

    async def write(self, buffer, timeout=None):
        self._log["written"].append(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log["sni"].append(server_hostname)
        return self


class _RecordingBackend(httpcore.AsyncMockBackend):
    """Mock network: answers every connection with ``responses`` in turn."""

    def __init__(self, responses: list[list[bytes]]):
        super().__init__([])
        self._responses = list(responses)
        self.log: dict[str, list] = {"connect": [], "written": [], "sni": []}

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        self.log["connect"].append((host, port))
        buf = self._responses.pop(0) if self._responses else []
        return _RecordingStream(list(buf), self.log)


_OK = [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n", b"\r\n", b"ok"]


def _redirect_to(location: str) -> list[bytes]:
    return [
        b"HTTP/1.1 302 Found\r\n",
        f"Location: {location}\r\n".encode(),
        b"Content-Length: 0\r\n",
        b"\r\n",
    ]


def _client(backend: _RecordingBackend, **kw) -> httpx.AsyncClient:
    return netguard.guarded_async_client(network_backend=backend, **kw)


# ---- the address rule --------------------------------------------------------


BLOCKED = [
    ("127.0.0.1", "loopback"),
    ("127.8.9.10", "loopback /8"),
    ("::1", "IPv6 loopback"),
    ("10.1.2.3", "RFC1918 10/8"),
    ("172.16.0.1", "RFC1918 172.16/12"),
    ("172.31.255.254", "RFC1918 172.16/12 top"),
    ("192.168.1.1", "RFC1918 192.168/16"),
    ("169.254.169.254", "link-local / cloud metadata"),
    ("169.254.0.1", "link-local"),
    ("100.64.0.1", "CGNAT 100.64/10"),
    ("100.127.255.254", "CGNAT top"),
    ("fc00::1", "ULA fc00::/7"),
    ("fd12:3456::1", "ULA fd00::/8"),
    ("fe80::1", "IPv6 link-local"),
    ("224.0.0.1", "IPv4 multicast"),
    ("ff02::1", "IPv6 multicast"),
    ("0.0.0.0", "unspecified"),
    ("::", "IPv6 unspecified"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ("::ffff:169.254.169.254", "IPv4-mapped metadata"),
    ("::ffff:10.0.0.1", "IPv4-mapped RFC1918"),
    ("0.1.2.3", "this-network 0/8"),
    ("64:ff9b::7f00:1", "NAT64 of loopback"),
    ("64:ff9b::a9fe:a9fe", "NAT64 of metadata"),
    ("2002:7f00:1::", "6to4 of loopback"),
]


@pytest.mark.parametrize(("ip", "label"), BLOCKED, ids=[b[1] for b in BLOCKED])
async def test_blocked_ranges_are_refused_by_name(monkeypatch, ip, label):
    _fake_dns(monkeypatch, {"evil.example": [ip]})
    with pytest.raises(EgressRefused) as ei:
        await netguard.vet_host("evil.example", 80)
    msg = str(ei.value)
    assert msg.startswith("refused: evil.example resolves to a private address")
    assert ip in msg
    assert "PRIMER_EGRESS_ALLOW" in msg


@pytest.mark.parametrize(("ip", "label"), BLOCKED, ids=[b[1] for b in BLOCKED])
async def test_blocked_ranges_are_refused_as_literals(monkeypatch, ip, label):
    _fake_dns(monkeypatch, {})
    with pytest.raises(EgressRefused):
        await netguard.vet_host(ip, 80)


async def test_public_addresses_pass(monkeypatch):
    _fake_dns(monkeypatch, {"ok.example": [PUBLIC_V4, PUBLIC_V6]})
    assert await netguard.vet_host("ok.example", 443) == [PUBLIC_V4, PUBLIC_V6]


async def test_one_public_and_one_private_record_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {"mixed.example": [PUBLIC_V4, "10.0.0.7"]})
    with pytest.raises(EgressRefused) as ei:
        await netguard.vet_host("mixed.example", 80)
    assert "10.0.0.7" in str(ei.value)


async def test_unresolvable_host_is_a_request_error(monkeypatch):
    _fake_dns(monkeypatch, {"gone.example": []})
    with pytest.raises(httpx.RequestError):
        await netguard.vet_host("gone.example", 80)


# ---- the operator allowlist --------------------------------------------------


async def test_allowlist_cidr_lets_an_internal_range_through(monkeypatch):
    _fake_dns(monkeypatch, {"svc.internal": ["10.20.0.5"]})
    netguard.configure_egress_allow(["10.20.0.0/16"])
    assert await netguard.vet_host("svc.internal", 80) == ["10.20.0.5"]


async def test_allowlist_cidr_does_not_open_other_ranges(monkeypatch):
    _fake_dns(monkeypatch, {"meta.example": ["169.254.169.254"]})
    netguard.configure_egress_allow(["10.20.0.0/16"])
    with pytest.raises(EgressRefused):
        await netguard.vet_host("meta.example", 80)


async def test_allowlist_single_ip(monkeypatch):
    _fake_dns(monkeypatch, {})
    netguard.configure_egress_allow(["127.0.0.1"])
    assert await netguard.vet_host("127.0.0.1", 80) == ["127.0.0.1"]
    with pytest.raises(EgressRefused):
        await netguard.vet_host("127.0.0.2", 80)


async def test_allowlist_host_name_is_exact_and_case_insensitive(monkeypatch):
    _fake_dns(monkeypatch, {
        "Registry.Internal": ["10.0.0.9"],
        "other.internal": ["10.0.0.9"],
    })
    netguard.configure_egress_allow(["registry.internal."])
    assert await netguard.vet_host("Registry.Internal", 443) == ["10.0.0.9"]
    with pytest.raises(EgressRefused):
        await netguard.vet_host("other.internal", 443)


def test_allowlist_rejects_garbage_entries():
    with pytest.raises(ValueError):
        netguard.parse_egress_allow(["not a host!"])
    with pytest.raises(ValueError):
        netguard.parse_egress_allow(["10.0.0.0/33"])


# ---- the httpx client: pinning, Host/SNI, redirects, scheme --------------------


async def test_public_url_connects_to_the_vetted_ip_with_the_original_host(monkeypatch):
    calls = _fake_dns(monkeypatch, {"public.example": [PUBLIC_V4]})
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        r = await c.get("http://public.example/path")
    assert r.status_code == 200
    assert r.text == "ok"
    # The socket was opened to the vetted IP, not to a second lookup.
    assert backend.log["connect"] == [(PUBLIC_V4, 80)]
    assert calls == ["public.example"]
    written = b"".join(backend.log["written"])
    assert b"Host: public.example" in written


async def test_https_keeps_the_host_name_for_sni(monkeypatch):
    _fake_dns(monkeypatch, {"public.example": [PUBLIC_V4]})
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        r = await c.get("https://public.example/")
    assert r.status_code == 200
    assert backend.log["connect"] == [(PUBLIC_V4, 443)]
    assert backend.log["sni"] == ["public.example"]


async def test_private_url_never_opens_a_socket(monkeypatch):
    _fake_dns(monkeypatch, {"metadata.example": ["169.254.169.254"]})
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        with pytest.raises(EgressRefused) as ei:
            await c.get("http://metadata.example/latest/meta-data/")
    assert backend.log["connect"] == []
    assert "169.254.169.254" in str(ei.value)


async def test_ip_literal_url_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {})
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        with pytest.raises(EgressRefused):
            await c.get("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(EgressRefused):
            await c.get("http://[::ffff:127.0.0.1]:8080/")
    assert backend.log["connect"] == []


async def test_redirect_to_a_private_address_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {
        "public.example": [PUBLIC_V4],
        "internal.example": ["10.0.0.5"],
    })
    backend = _RecordingBackend([_redirect_to("http://internal.example/admin"), _OK])
    async with _client(backend, follow_redirects=True) as c:
        with pytest.raises(EgressRefused) as ei:
            await c.get("http://public.example/r")
    assert backend.log["connect"] == [(PUBLIC_V4, 80)]
    assert "internal.example" in str(ei.value)


async def test_redirect_to_a_private_ip_literal_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {"public.example": [PUBLIC_V4]})
    backend = _RecordingBackend(
        [_redirect_to("http://169.254.169.254/latest/meta-data/"), _OK]
    )
    async with _client(backend, follow_redirects=True) as c:
        with pytest.raises(EgressRefused):
            await c.get("http://public.example/r")
    assert backend.log["connect"] == [(PUBLIC_V4, 80)]


async def test_redirect_to_a_public_host_is_followed(monkeypatch):
    _fake_dns(monkeypatch, {
        "a.example": [PUBLIC_V4],
        "b.example": ["8.8.8.8"],
    })
    backend = _RecordingBackend([_redirect_to("http://b.example/x"), _OK])
    async with _client(backend, follow_redirects=True) as c:
        r = await c.get("http://a.example/")
    assert r.status_code == 200
    assert backend.log["connect"] == [(PUBLIC_V4, 80), ("8.8.8.8", 80)]


async def test_allowlisted_internal_host_is_reached(monkeypatch):
    _fake_dns(monkeypatch, {"svc.internal": ["10.20.0.5"]})
    netguard.configure_egress_allow(["svc.internal"])
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        r = await c.get("http://svc.internal:8080/")
    assert r.status_code == 200
    assert backend.log["connect"] == [("10.20.0.5", 8080)]


async def test_non_http_scheme_is_refused():
    backend = _RecordingBackend([_OK])
    async with _client(backend) as c:
        with pytest.raises(httpx.UnsupportedProtocol):
            await c.get("ftp://public.example/file")
    assert backend.log["connect"] == []


# ---- timeouts: resolution and every attempt share the connect deadline ------------


class _DeadFirstBackend(httpcore.AsyncMockBackend):
    """The first address is blackholed (waits out its timeout); others answer."""

    def __init__(self, dead: str):
        super().__init__([])
        self._dead = dead
        self.attempts: list[tuple[str, float | None]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        self.attempts.append((host, timeout))
        if host == self._dead:
            await anyio.sleep(timeout if timeout is not None else 3600)
            raise httpcore.ConnectTimeout("blackholed")
        return httpcore.AsyncMockStream(list(_OK))


async def test_a_dead_first_address_does_not_consume_the_whole_budget(monkeypatch):
    _fake_dns(monkeypatch, {"two.example": ["203.0.113.250", PUBLIC_V4]})
    # 203.0.113.0/24 is TEST-NET-3 (not global), so allowlist it: the test is
    # about the timeout split, not the address rule.
    netguard.configure_egress_allow(["203.0.113.250"])
    backend = _DeadFirstBackend(dead="203.0.113.250")
    started = time.monotonic()
    async with _client(backend, timeout=httpx.Timeout(5.0, connect=0.6)) as c:
        r = await c.get("http://two.example/")
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert [h for h, _ in backend.attempts] == ["203.0.113.250", PUBLIC_V4]
    first_timeout = backend.attempts[0][1]
    assert first_timeout is not None and first_timeout <= 0.31, backend.attempts
    assert elapsed < 0.55, elapsed


async def test_slow_resolution_counts_against_the_connect_timeout(monkeypatch):
    async def _slow_resolve(host, port):
        await anyio.sleep(5)
        return [PUBLIC_V4]

    monkeypatch.setattr(netguard, "_resolve", _slow_resolve)
    backend = _RecordingBackend([_OK])
    started = time.monotonic()
    async with _client(backend, timeout=httpx.Timeout(5.0, connect=0.3)) as c:
        with pytest.raises(httpx.ConnectTimeout):
            await c.get("http://slow.example/")
    assert time.monotonic() - started < 1.5
    assert backend.log["connect"] == []


def test_egress_refused_is_an_httpx_request_error():
    """Existing ``except httpx.RequestError`` handlers catch a refusal."""
    assert issubclass(EgressRefused, httpx.RequestError)


# ---- the aiohttp resolver (url file mounts) -------------------------------------


async def test_aiohttp_resolver_refuses_private_and_pins_public(monkeypatch):
    _fake_dns(monkeypatch, {
        "public.example": [PUBLIC_V4],
        "internal.example": ["192.168.0.10"],
    })
    resolver = netguard.GuardedResolver()
    out = await resolver.resolve("public.example", 443)
    assert [r["host"] for r in out] == [PUBLIC_V4]
    assert out[0]["hostname"] == "public.example"
    assert out[0]["port"] == 443
    with pytest.raises(EgressRefused):
        await resolver.resolve("internal.example", 443)
    await resolver.close()
