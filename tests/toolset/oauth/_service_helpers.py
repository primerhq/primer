"""Shared pieces of the ``ToolsetOAuthService`` tests (plan 07, Tasks 7 and 8): a settable clock, toolset rows, stored token rows, and a whole
browser sign-in against the hermetic mock, driven without a browser."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from primer.model.mcp_oauth import McpOAuthTokenSet
from primer.model.provider import Toolset
from primer.toolset.oauth.service import OAuthTarget, ToolsetAuthStatus, ToolsetOAuthService, config_fingerprint
from primer.toolset.oauth.store import McpOAuthStore

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
KEY = "service-test-key-0123456789"
OTHER_KEY = "another-service-key-0123456789"
#: Nothing listens there: a test reads the provider's redirect and finishes the sign-in itself.
REDIRECT = "http://127.0.0.1:9/v1/toolsets/kb/oauth/callback"
#: The nonce of the browser that starts a sign-in (the route's cookie, audit G4-01); the callback must present it.
BINDING = "the-browser-that-started-the-sign-in-0123456789"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class Clock:
    """The service's clock, moved by the test. The mock's token lifetimes run on real time, so a test that lets a stored token expire moves this clock."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def make_toolset(url: str, *, oauth: dict[str, Any] | None, toolset_id: str = "kb", headers: dict[str, str] | None = None, transport: str = "http") -> Toolset:
    http_config: dict[str, Any] = {"url": url, "headers": headers or {}}
    if oauth is not None:
        http_config["oauth"] = oauth
    return Toolset(id=toolset_id, provider="mcp", config={"transport": transport, "config": http_config})


def target_of(toolset: Toolset) -> OAuthTarget:
    target = OAuthTarget.from_toolset(toolset)
    assert target is not None, "not an OAuth toolset"
    return target


def token_row(store: McpOAuthStore, target: OAuthTarget, **over: Any) -> McpOAuthTokenSet:
    """A stored token set for ``target``, as a connect would have written it, sealed by ``store``."""
    base: dict[str, Any] = {
        "id": target.toolset_id,
        "grant": target.oauth.grant,
        "access_token_enc": store.seal("at-plain"),
        "refresh_token_enc": store.seal("rt-plain"),
        "expires_at": NOW + timedelta(hours=1),
        "scope": "mcp",
        "issuer": "https://as.example",
        "token_endpoint": "https://as.example/token",
        "client_id": "c1",
        "client_source": "registered",
        "config_fingerprint": config_fingerprint(target),
        "connected_at": NOW - timedelta(hours=2),
        "connected_by": "admin",
        "refreshed_at": NOW - timedelta(minutes=5),
    }
    base.update(over)
    return McpOAuthTokenSet(**base)


@contextmanager
def quiet_httpx() -> Iterator[None]:
    """The test's own requests (following an authorize URL) put the state and the code in what httpx logs (the URL) and httpcore traces (the redirect's
    ``Location`` header): keep them out of a captured log, which must hold only what the service itself caused."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def authorize(url: str) -> dict[str, str]:
    """GET an authorize URL of the mock without following its redirect (the mock consents at once): the redirect's query, one value per name."""
    with quiet_httpx():
        response = httpx.get(url, follow_redirects=False)
    assert response.status_code == 302, response.text
    return {name: values[0] for name, values in parse_qs(urlsplit(response.headers["location"]).query).items()}


async def connect(svc: ToolsetOAuthService, toolset: Toolset, *, redirect_uri: str = REDIRECT, started_by: str = "admin") -> ToolsetAuthStatus:
    """A whole browser sign-in: start, the provider's consent, the callback (from the browser that started it)."""
    start = await svc.start_sign_in(target_of(toolset), redirect_uri=redirect_uri, started_by=started_by, browser_binding=BINDING)
    back = authorize(start.authorize_url)
    return await svc.complete_sign_in(toolset, toolset_id=toolset.id, state=back["state"], code=back["code"], error=None, browser_binding=BINDING)


def mcp_answers(url: str, token: str) -> int:
    """The status the mock's MCP endpoint answers an ``initialize`` carrying ``token``."""
    with quiet_httpx():
        return httpx.post(url, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}).status_code
