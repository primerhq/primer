"""SSRF-03 / SSRF-04: the production outbound paths refuse internal addresses.

Each test drives the object the app actually builds (``build_web_toolset``
with no injected client, ``LocalAdapter()`` with no injected client, and
``resolve_file_sources`` with the real session hook), so a regression that
drops the guard from one of those constructors fails here.

No external network: the targets are loopback literals (refused before any
socket is opened once the guard is in place) or names routed through a
monkeypatched resolver.
"""

from __future__ import annotations

import pytest

from primer.model.workspace import FileMount, _UrlSource
from primer.model.yield_ import ToolContext
from primer.toolset.web import build_web_toolset
from primer.web_fetch.adapter import WebFetchProviderError
from primer.web_fetch.local import LocalAdapter
from primer.workspace.files import resolve_file_sources


# A loopback port nothing listens on: an unguarded client gets a fast local
# ConnectError, a guarded one a refusal that names the address.
LOOPBACK_URL = "http://127.0.0.1:1/admin"


@pytest.fixture(autouse=True)
def _reset_allowlist():
    try:
        from primer.common import netguard
    except ImportError:  # the unfixed tree: let each test fail on its own
        yield
        return
    netguard.configure_egress_allow([])
    yield
    netguard.configure_egress_allow([])


class _NoService:
    async def search(self, **_):  # pragma: no cover - never called
        raise AssertionError("not used")

    async def fetch(self, **_):  # pragma: no cover - never called
        raise AssertionError("not used")


class _Ws:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    async def write_file(self, path: str, content: bytes) -> None:
        self.writes.append((path, content))


class _WsRegistry:
    def __init__(self) -> None:
        self.ws = _Ws()

    async def get_workspace(self, workspace_id: str):
        return self.ws


def _assert_refusal(text: str, address: str) -> None:
    assert "refused:" in text, text
    assert "private address" in text, text
    assert address in text, text
    assert "PRIMER_EGRESS_ALLOW" in text, text


async def test_http_request_refuses_loopback():
    ts = build_web_toolset(web_search_service=_NoService(), web_fetch_service=_NoService())
    result = await ts.call(tool_name="http_request", arguments={"url": LOOPBACK_URL})
    assert result.is_error
    _assert_refusal(result.output, "127.0.0.1")


async def test_http_request_refuses_a_name_that_resolves_to_metadata(monkeypatch):
    from primer.common import netguard

    async def _resolve(host, port):
        return ["169.254.169.254"] if host == "metadata.example" else [host]

    monkeypatch.setattr(netguard, "_resolve", _resolve)
    ts = build_web_toolset(web_search_service=_NoService(), web_fetch_service=_NoService())
    result = await ts.call(
        tool_name="http_request",
        arguments={"url": "http://metadata.example/latest/meta-data/"},
    )
    assert result.is_error
    _assert_refusal(result.output, "169.254.169.254")
    assert "metadata.example" in result.output


async def test_http_request_allowlisted_address_is_not_refused():
    from primer.common import netguard

    netguard.configure_egress_allow(["127.0.0.1/32"])
    ts = build_web_toolset(web_search_service=_NoService(), web_fetch_service=_NoService())
    result = await ts.call(tool_name="http_request", arguments={"url": LOOPBACK_URL})
    # Nothing listens on port 1, so the call still fails, but on the
    # connection, not on the guard.
    assert result.is_error
    assert "refused:" not in result.output
    assert "ConnectError" in result.output


async def test_download_refuses_loopback_and_writes_nothing():
    reg = _WsRegistry()
    ts = build_web_toolset(
        web_search_service=_NoService(),
        web_fetch_service=_NoService(),
        workspace_registry=reg,
    )
    result = await ts.call(
        tool_name="download",
        arguments={"url": "http://127.0.0.1:1/secrets.json"},
        ctx=ToolContext(tool_call_id="tc", session_id="s", workspace_id="ws"),
    )
    assert result.is_error
    _assert_refusal(result.output, "127.0.0.1")
    assert reg.ws.writes == []


async def test_local_web_fetch_adapter_refuses_loopback():
    adapter = LocalAdapter()
    try:
        with pytest.raises(WebFetchProviderError) as ei:
            await adapter.fetch(url=LOOPBACK_URL)
    finally:
        await adapter.aclose()
    _assert_refusal(str(ei.value), "127.0.0.1")


async def test_url_file_mount_refuses_loopback_literal():
    fm = FileMount(path="x", source=_UrlSource(url=LOOPBACK_URL))
    with pytest.raises(RuntimeError) as ei:
        await resolve_file_sources([fm])
    _assert_refusal(str(ei.value), "127.0.0.1")


async def test_url_file_mount_refuses_a_name_that_resolves_private(monkeypatch):
    from primer.common import netguard

    async def _resolve(host, port):
        return ["10.0.0.5"] if host == "internal.example" else [host]

    monkeypatch.setattr(netguard, "_resolve", _resolve)
    fm = FileMount(path="x", source=_UrlSource(url="http://internal.example/x"))
    with pytest.raises(RuntimeError) as ei:
        await resolve_file_sources([fm])
    _assert_refusal(str(ei.value), "10.0.0.5")


def test_app_config_egress_allow_defaults_empty_and_validates():
    from primer.api.config import AppConfig

    assert AppConfig().egress_allow == []
    cfg = AppConfig(egress_allow=["10.0.0.0/8", "registry.internal"])
    assert cfg.egress_allow == ["10.0.0.0/8", "registry.internal"]
    with pytest.raises(ValueError):
        AppConfig(egress_allow=["not a host!"])


async def test_lifespan_installs_the_configured_allowlist(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from primer.api.app import _make_lifespan
    from primer.api.config import AppConfig
    from primer.common import netguard
    from primer.common.netguard import EgressRefused
    from primer.model.scheduler import RuntimeMode

    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(EgressRefused):
        await netguard.vet_host("127.0.0.1", 80)
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        auto_bootstrap=False,
        egress_allow=["127.0.0.1/32"],
    )
    app = FastAPI(lifespan=_make_lifespan(cfg))
    async with app.router.lifespan_context(app):
        assert await netguard.vet_host("127.0.0.1", 80) == ["127.0.0.1"]


class _FakeResp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b""):
        self.status = status
        self.headers = headers
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakeSession:
    """aiohttp-shaped session answering each URL from ``routes``."""

    def __init__(self, routes: dict[str, _FakeResp]):
        self.routes = routes
        self.seen: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def get(self, url, **kw):
        assert kw.get("allow_redirects") is False
        self.seen.append(url)
        return self.routes[url]


async def test_url_file_mount_refuses_a_redirect_to_a_private_literal(monkeypatch):
    """Redirects are followed by hand so each hop's host is checked."""
    session = _FakeSession({
        "https://public.example/f": _FakeResp(
            302, {"Location": "http://169.254.169.254/latest/"}
        ),
    })
    monkeypatch.setattr("primer.workspace.files._http_session", lambda: session)
    fm = FileMount(path="x", source=_UrlSource(url="https://public.example/f"))
    with pytest.raises(RuntimeError) as ei:
        await resolve_file_sources([fm])
    _assert_refusal(str(ei.value), "169.254.169.254")
    assert session.seen == ["https://public.example/f"]


async def test_url_file_mount_follows_a_public_redirect(monkeypatch):
    session = _FakeSession({
        "https://public.example/f": _FakeResp(302, {"Location": "/g"}),
        "https://public.example/g": _FakeResp(200, {}, b"payload"),
    })
    monkeypatch.setattr("primer.workspace.files._http_session", lambda: session)
    fm = FileMount(path="x", source=_UrlSource(url="https://public.example/f"))
    out = await resolve_file_sources([fm])
    assert out[0].content == b"payload"
    assert session.seen == ["https://public.example/f", "https://public.example/g"]

