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
