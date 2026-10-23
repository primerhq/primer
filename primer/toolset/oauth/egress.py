"""Where the OAuth calls of an MCP toolset's sign-in may connect (audit G4-08).

The MCP server names most of what primer calls during a sign-in and a refresh: the protected-resource document of its 401 challenge, its
authorization server, and that server's metadata, token, registration and revocation endpoints. The toolset itself is user-creatable, so an admin's
Connect (and every refresh after it) would otherwise make primer call link-local, loopback and private addresses of the MCP server's choosing.

So the service's HTTP client (:func:`oauth_http_client`) routes each request by its host AND port (the port the scheme defaults to when the URL names
none):

* a request to the MCP server's OWN host and port, or to the host and port of a token endpoint the toolset configures, goes out as before. The MCP
  URL itself is not guarded (the toolset's author chose it, and the MCP client connects to it anyway), and an authorization server served from the
  same host and port stays reachable;
* a request to ANY OTHER host, or to another port of the same host (another service on that machine), passes :mod:`primer.common.ssrf`'s private-destination policy at connect time
  (:func:`primer.common.ssrf.public_addresses`, through :class:`primer.common.netguard.GuardedNetworkBackend`), and connects to the address that
  passed, so a name re-bound between the check and the connect cannot slip through. A refused destination raises
  :class:`primer.common.ssrf.BlockedDestinationError`, which :mod:`primer.toolset.oauth.protocol` turns into a refusal naming the endpoint.

The operator's opt-in for that policy, ``workspace_allow_private_url_sources`` (``PRIMER_WORKSPACE_ALLOW_PRIVATE_URL_SOURCES``), lets private
addresses through here too; no other setting exists for it.

The endpoints a request may reach directly are those of the toolset the call is FOR: :func:`calls_for` sets them for the code inside it (a context
variable, so the concurrent calls of other toolsets keep their own, and a task started inside it inherits them). Outside any toolset nothing is
direct. Each route keeps its own connection pool, so a connection opened directly to one toolset's MCP endpoint is never reused for another
toolset's request to it, which goes through the guard.

Like the web tools' guarded client, an explicit transport means httpx does not apply the ``HTTP(S)_PROXY`` environment to these calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpcore
import httpx

from primer.common import ssrf
from primer.common.netguard import guarded_transport, transport_over

__all__ = ["OAuthEgressTransport", "calls_for", "direct_endpoints", "oauth_http_client"]

Endpoint = tuple[str, int]

_DEFAULT_PORTS = {"http": 80, "https": 443}
_DIRECT: ContextVar[frozenset[Endpoint]] = ContextVar("primer_mcp_oauth_direct_endpoints", default=frozenset())


def _endpoint_of(url: httpx.URL) -> Endpoint | None:
    """``(host, port)`` of ``url``, the port the scheme defaults to when it names none; ``None`` without a host or a known port."""
    port = url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)
    return (url.host, port) if url.host and port is not None else None


def _endpoint(url: str | None) -> Endpoint | None:
    if not url:
        return None
    try:
        return _endpoint_of(httpx.URL(url))
    except (httpx.InvalidURL, TypeError, ValueError):
        return None


@contextmanager
def calls_for(server_url: str, *configured_urls: str | None) -> Iterator[None]:
    """Inside this block, requests to the host and port of ``server_url`` (the toolset's MCP URL) and of each configured URL (a token endpoint the
    toolset names) go out directly; every other endpoint is vetted."""
    endpoints = frozenset(endpoint for endpoint in (_endpoint(url) for url in (server_url, *configured_urls)) if endpoint)
    token = _DIRECT.set(endpoints)
    try:
        yield
    finally:
        _DIRECT.reset(token)


def direct_endpoints() -> frozenset[Endpoint]:
    """The ``(host, port)`` pairs the current call may reach without the guard."""
    return _DIRECT.get()


async def _vet(host: str, port: int) -> list[str]:
    """:func:`primer.common.ssrf.public_addresses`, with a name that does not resolve reported as httpx reports a failed connect."""
    try:
        return await ssrf.public_addresses(host, port)
    except OSError as exc:
        raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from exc


class OAuthEgressTransport(httpx.AsyncBaseTransport):
    """Sends a request to one of :func:`direct_endpoints` directly and every other request through the private-destination guard."""

    def __init__(self, network_backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._direct = httpx.AsyncHTTPTransport() if network_backend is None else transport_over(network_backend)
        self._vetted = guarded_transport(network_backend=network_backend, vet=_vet)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._direct if _endpoint_of(request.url) in direct_endpoints() else self._vetted
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._direct.aclose()
        await self._vetted.aclose()


def oauth_http_client(*, timeout: float, network_backend: httpcore.AsyncNetworkBackend | None = None) -> httpx.AsyncClient:
    """The OAuth client of :class:`~primer.toolset.oauth.service.ToolsetOAuthService`: redirects are never followed, every request is routed by
    :class:`OAuthEgressTransport`. ``network_backend`` is for tests (an httpcore mock under both routes)."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=OAuthEgressTransport(network_backend))
