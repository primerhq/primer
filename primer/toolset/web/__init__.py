"""Internal ``web`` toolset — built-in, immutable.

Construct with :func:`build_web_toolset`; the returned
:class:`InternalToolsetProvider` exposes two tools:

* ``web-search`` — backed by :class:`WebSearchBackend` (default
  :class:`DuckDuckGoBackend`, no API key).
* ``http-request`` — backed by :class:`httpx.AsyncClient`.

The toolset is "internal" in two senses:

1. The implementation lives in this Python package — there is no
   config row that, if removed, would un-register the tools.
2. :class:`InternalToolsetProvider` takes a defensive copy of its
   registry at construction; the registry cannot be mutated through
   the provider object after the fact.

A future "toolset registry" admin surface (when one lands) should
mark internally-built providers with a flag preventing ``delete()``.
"""

from __future__ import annotations

import httpx

from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.web.backends import (
    DuckDuckGoBackend,
    SafeSearchLevel,
    SearchHit,
    WebSearchBackend,
)
from primer.toolset.web.tools import (
    HttpMethod,
    HttpRequestArgs,
    WebSearchArgs,
    make_http_request_descriptor,
    make_http_request_handler,
    make_web_search_descriptor,
    make_web_search_handler,
)


_DEFAULT_RESPONSE_BODY_BYTE_CAP = 1_000_000  # 1 MB


def build_web_toolset(
    *,
    toolset_id: str = "web",
    backend: WebSearchBackend | None = None,
    http_client: httpx.AsyncClient | None = None,
    response_body_byte_cap: int = _DEFAULT_RESPONSE_BODY_BYTE_CAP,
) -> InternalToolsetProvider:
    """Build the immutable ``web`` toolset provider.

    Parameters
    ----------
    toolset_id
        Wire id stamped onto every :class:`Tool` descriptor and used by
        the :class:`ToolExecutionManager` to route calls back here.
        Defaults to ``"web"``.
    backend
        Optional :class:`WebSearchBackend` for the ``web-search`` tool.
        When ``None``, defaults to a fresh :class:`DuckDuckGoBackend`
        (no API key, pure Python).
    http_client
        Optional :class:`httpx.AsyncClient` used by the ``http-request``
        tool. When ``None``, the factory constructs a default async
        client. Callers running long-lived applications should pass a
        shared client and manage its lifecycle (``await client.aclose()``).
    response_body_byte_cap
        Maximum bytes returned in ``http-request`` response bodies;
        anything past the cap is truncated and ``"truncated": true``
        is set in the result. Defaults to 1 MB.

    Returns
    -------
    InternalToolsetProvider
        A ready-to-register provider with ``web-search`` and
        ``http-request`` tools wired in.
    """
    chosen_backend: WebSearchBackend = backend or DuckDuckGoBackend()
    chosen_client: httpx.AsyncClient = (
        http_client if http_client is not None else httpx.AsyncClient()
    )

    registry: dict[str, tuple] = {
        "web-search": (
            make_web_search_descriptor(toolset_id),
            make_web_search_handler(chosen_backend),
        ),
        "http-request": (
            make_http_request_descriptor(toolset_id),
            make_http_request_handler(
                http_client=chosen_client,
                response_body_byte_cap=response_body_byte_cap,
            ),
        ),
    }
    return InternalToolsetProvider(toolset_id, registry)


__all__ = [
    "DuckDuckGoBackend",
    "HttpMethod",
    "HttpRequestArgs",
    "SafeSearchLevel",
    "SearchHit",
    "WebSearchArgs",
    "WebSearchBackend",
    "build_web_toolset",
]
