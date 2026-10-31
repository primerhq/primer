"""The MCP client's HTTP connections for a toolset an admin did not author go through the egress guard (review of workspaces, W25).

``mcp``'s transports take an ``httpx2.AsyncClient`` (``streamable_http_client(http_client=)``) or a factory for one (``sse_client(httpx_client_factory=)``).
:func:`guarded_mcp_http_client` is :func:`mcp.shared._httpx_utils.create_mcp_http_client` with a transport whose every connection is vetted by
:class:`primer.common.netguard.GuardedNetworkBackend`: the host is resolved, any internal answer is refused (so a name re-bound after the save is
refused too, :class:`~primer.common.netguard.EgressRefused`), and the connection goes to the address that passed. ``PRIMER_EGRESS_ALLOW`` still opts
internal servers in. The pool swap is the one :func:`primer.common.netguard.transport_over` makes for plain httpx; ``mcp`` runs on ``httpx2``,
whose pool is ``httpcore2``'s: the guard vets, and ``httpcore2``'s own backend opens the connection.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpcore2
import httpx2
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from primer.common.netguard import GuardedNetworkBackend


def _guarded_transport(network_backend: Any = None) -> httpx2.AsyncHTTPTransport:
    transport = httpx2.AsyncHTTPTransport()
    # httpx2 exposes no network_backend knob; swap in a pool built with one. Fail loudly if an upgrade changes what the transport holds, rather
    # than silently keeping an unguarded pool.
    if not isinstance(transport._pool, httpcore2.AsyncConnectionPool):  # noqa: SLF001
        raise RuntimeError(
            f"mcp egress guard: httpx2.AsyncHTTPTransport._pool is {type(transport._pool).__name__}, not httpcore2.AsyncConnectionPool"  # noqa: SLF001
        )
    transport._pool = httpcore2.AsyncConnectionPool(  # noqa: SLF001
        ssl_context=httpx2.create_ssl_context(),
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=5.0,
        network_backend=GuardedNetworkBackend(network_backend or httpcore2.AnyIOBackend()),  # type: ignore[arg-type]  # duck-typed: httpcore2's backend
    )
    return transport


def guarded_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    *,
    network_backend: Any = None,
) -> httpx2.AsyncClient:
    """An ``httpx2`` client with the MCP transports' default timeouts whose connections are all vetted (``network_backend`` is for tests)."""
    if timeout is None:
        timeout = httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    kwargs: dict[str, Any] = {"timeout": timeout, "transport": _guarded_transport(network_backend)}
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx2.AsyncClient(**kwargs)


async def author_is_trusted(storage_provider: Any, toolset: Any) -> bool:
    """Whether an admin authored ``toolset`` (so its server may be an internal one): any of its owners is an enabled admin, or it has no owner (a
    platform-seeded or harness-installed row; harness installs are admin-only). The row is read again so a change of owners or of the owner's
    role applies on the next dispatch. A toolset owned only by non-admins is guarded."""
    from primer.model.provider import Toolset
    from primer.model.user import User

    row = await storage_provider.get_storage(Toolset).get(toolset.id) or toolset
    access = getattr(row, "access", None)
    owners = list(getattr(access, "owners", None) or [])
    if not owners:
        return True
    users = storage_provider.get_storage(User)
    for owner_id in owners:
        user = await users.get(owner_id)
        if user is not None and user.role == "admin" and not user.disabled:
            return True
    return False


__all__ = ["author_is_trusted", "guarded_mcp_http_client"]
