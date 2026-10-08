"""A ``kind=url`` file source does not reach private, loopback, link-local or metadata addresses (AUTHZ-05 part b).

The platform fetches the URL when it materialises a workspace, so before this change any template author could make the
server read an internal endpoint (the cloud metadata service, a sidecar on loopback) and land the answer in their
workspace. The fetch now refuses such a destination: an IP literal before any connection, a host name at resolution
time (so a name re-bound to a private address between a check and the connect cannot slip through), and every redirect
hop.
"""

from __future__ import annotations

import socket

import pytest

from primer.model.workspace import FileMount, _UrlSource
from primer.workspace.files import resolve_file_sources


def _url_mount(url: str) -> FileMount:
    return FileMount(path="x", source=_UrlSource(url=url))


def _no_session():
    raise AssertionError("the fetch opened a connection to a refused destination")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5:8080/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/",
])
async def test_a_url_source_at_a_private_ip_literal_is_refused_before_any_connection(monkeypatch, url) -> None:
    monkeypatch.setattr("primer.workspace.files._http_session", _no_session)

    with pytest.raises(RuntimeError, match="refused"):
        await resolve_file_sources([_url_mount(url)])


async def test_a_redirect_to_a_private_address_is_refused(monkeypatch) -> None:
    fetched: list[str] = []

    class _Resp:
        def __init__(self, url: str) -> None:
            self._url = url
            self.status = 302 if "example.com" in url else 200
            self.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        async def read(self) -> bytes:
            return b"metadata"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url, **_):
            fetched.append(str(url))
            return _Resp(str(url))

    monkeypatch.setattr("primer.workspace.files._http_session", lambda: _Session())

    with pytest.raises(RuntimeError, match="refused"):
        await resolve_file_sources([_url_mount("https://example.com/x")])
    assert fetched == ["https://example.com/x"]


async def test_the_resolver_refuses_a_name_that_resolves_to_a_private_address() -> None:
    from primer.common.ssrf import BlockedDestinationError, PublicOnlyResolver

    class _Inner:
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": "10.1.2.3", "port": port, "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            return None

    resolver = PublicOnlyResolver(inner=_Inner())

    with pytest.raises(BlockedDestinationError, match="10.1.2.3"):
        await resolver.resolve("internal.example", 80)


async def test_the_resolver_passes_a_public_address() -> None:
    from primer.common.ssrf import PublicOnlyResolver

    class _Inner:
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": "93.184.216.34", "port": port, "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            return None

    out = await PublicOnlyResolver(inner=_Inner()).resolve("example.com", 443)

    assert [r["host"] for r in out] == ["93.184.216.34"]


@pytest.mark.parametrize("address", [
    "64:ff9b::7f00:1",        # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",     # NAT64 of 169.254.169.254
    "64:ff9b::a00:5",         # NAT64 of 10.0.0.5
    "::7f00:1",               # IPv4-compatible 127.0.0.1
    "::a9fe:a9fe",            # IPv4-compatible 169.254.169.254
    "::c0a8:101",             # IPv4-compatible 192.168.1.1
])
def test_an_embedded_private_ipv4_is_refused(address) -> None:
    from primer.common.ssrf import blocked_reason

    assert blocked_reason(address) is not None


@pytest.mark.parametrize("address", ["64:ff9b::808:808", "::808:808"])
def test_an_embedded_public_ipv4_is_judged_as_that_ipv4(address) -> None:
    from primer.common.ssrf import blocked_reason

    assert blocked_reason(address) is None


async def test_the_real_session_resolves_through_the_public_only_resolver() -> None:
    """Pins the wiring: a bare ``aiohttp.ClientSession()`` here would resolve with aiohttp's own resolver."""
    from primer.common.ssrf import PublicOnlyResolver
    from primer.workspace.files import _http_session

    session = _http_session()
    try:
        assert isinstance(session.connector._resolver, PublicOnlyResolver)  # noqa: SLF001
        assert session.trust_env is False
    finally:
        await session.close()


async def test_a_name_resolving_private_is_refused_through_the_real_session(monkeypatch) -> None:
    """Drives the real ``_http_session`` and ``_fetch_url``: the refusal the resolver raises during connect surfaces
    as the RuntimeError "refused". No DNS and no socket: aiohttp's default resolver is replaced by one that answers a
    private address."""
    import aiohttp.resolver

    class _PrivateResolver:
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": "10.9.8.7", "port": port, "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            return None

    monkeypatch.setattr(aiohttp.resolver, "DefaultResolver", _PrivateResolver)

    with pytest.raises(RuntimeError, match="refused") as ei:
        await resolve_file_sources([_url_mount("http://internal.example/x")])
    assert "10.9.8.7" in str(ei.value)


@pytest.mark.parametrize("address", [
    "::ffff:0:7f00:1",        # SIIT (IPv4-translated) 127.0.0.1
    "::ffff:0:a9fe:a9fe",     # SIIT 169.254.169.254
    "::ffff:0:a00:5",         # SIIT 10.0.0.5
])
def test_an_ipv4_translated_private_address_is_refused(address) -> None:
    from primer.common.ssrf import blocked_reason

    assert blocked_reason(address) is not None


def test_an_ipv4_translated_public_address_is_judged_as_that_ipv4() -> None:
    from primer.common.ssrf import blocked_reason

    assert blocked_reason("::ffff:0:808:808") is None


async def test_a_slow_url_source_times_out(monkeypatch) -> None:
    """Lead re-review of #473: a url source that never finishes must not hold materialisation; the whole fetch,
    redirects and body included, is bounded."""
    import asyncio

    import primer.workspace.files as files

    class _SlowResp:
        status = 200
        headers: dict = {}

        async def read(self) -> bytes:
            await asyncio.sleep(3600)
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url, **_):
            return _SlowResp()

    monkeypatch.setattr(files, "_http_session", lambda: _Session())
    monkeypatch.setattr(files, "_FETCH_TIMEOUT_S", 0.05)

    async with asyncio.timeout(10):  # the test body is bounded too
        with pytest.raises(RuntimeError, match="timed out"):
            await resolve_file_sources([_url_mount("https://example.com/slow")])


def test_the_fetch_timeout_is_a_minute() -> None:
    import primer.workspace.files as files

    assert files._FETCH_TIMEOUT_S == 60.0
