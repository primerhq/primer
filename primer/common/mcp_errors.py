"""Shared mcp / httpx exception classifier.

Used by :class:`primer.toolset.mcp.McpToolsetProvider` (and any future
adapter that talks to an MCP server). Maps the mcp SDK's
:class:`mcp.shared.exceptions.McpError` plus the underlying httpx
exceptions onto the primer exception hierarchy so callers see one
universal error surface regardless of which adapter raised.
"""

from __future__ import annotations

import httpx
from mcp.shared.exceptions import McpError

from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    PrimerError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)


def classify_mcp_exception(exc: Exception) -> PrimerError:
    """Map an mcp / httpx exception to the primer exception hierarchy.

    Mapping rules:

    | Source exception | primer exception |
    |---|---|
    | ``httpx.HTTPStatusError`` 401 / 403 | :class:`AuthenticationError` |
    | ``httpx.HTTPStatusError`` 429 | :class:`RateLimitError` |
    | ``httpx.HTTPStatusError`` 400 | :class:`BadRequestError` |
    | ``httpx.HTTPStatusError`` 5xx | :class:`ServerError` |
    | other ``httpx.HTTPStatusError`` | :class:`ProviderError` |
    | ``httpx.TimeoutException``, ``httpx.NetworkError`` | :class:`NetworkError` |
    | ``mcp.shared.exceptions.McpError`` | :class:`ProviderError` (carries the JSON-RPC error code) |
    | anything else | :class:`ProviderError` |

    Already-classified :class:`PrimerError` inputs pass through unchanged, and
    anyio ``(Base)ExceptionGroup`` wrappers (streamable-http / stdio transports
    run inside anyio task groups) are unwrapped to their most informative leaf
    so callers see e.g. "connection refused" instead of the opaque "unhandled
    errors in a TaskGroup".
    """
    # Idempotent: re-classifying at an outer layer is a no-op.
    if isinstance(exc, PrimerError):
        return exc
    # Unwrap anyio task-group wrappers to the real failure. Prefer an
    # already-classified PrimerError leaf, else classify the first Exception
    # leaf (typically the httpx.ConnectError behind a failed MCP connection).
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        pending: list[BaseException] = [exc]
        while pending:
            cur = pending.pop()
            if isinstance(cur, BaseExceptionGroup):
                pending.extend(cur.exceptions)
            else:
                leaves.append(cur)
        for leaf in leaves:
            if isinstance(leaf, PrimerError):
                return leaf
        for leaf in leaves:
            if isinstance(leaf, Exception):
                return classify_mcp_exception(leaf)
        return ProviderError(str(exc), cause=exc)
    if isinstance(exc, httpx.ConnectError):
        return NetworkError(
            f"could not connect to the MCP server: {exc}",
            cause=exc,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return AuthenticationError(
                f"MCP server rejected credentials ({status})",
                status_code=status,
                cause=exc,
            )
        if status == 429:
            return RateLimitError(
                "MCP server rate limit exceeded",
                status_code=status,
                cause=exc,
            )
        if status == 400:
            return BadRequestError(
                "MCP server rejected the request",
                status_code=status,
                cause=exc,
            )
        if status >= 500:
            return ServerError(
                f"MCP server error ({status})",
                status_code=status,
                cause=exc,
            )
        return ProviderError(
            f"MCP server returned status {status}",
            status_code=status,
            cause=exc,
        )
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return NetworkError(
            f"MCP network failure: {type(exc).__name__}",
            cause=exc,
        )
    if isinstance(exc, McpError):
        data = exc.error
        return ProviderError(
            data.message,
            code=str(data.code) if data.code is not None else None,
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)
