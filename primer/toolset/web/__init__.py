"""Internal ``web`` toolset — built-in, immutable.

Construct with :func:`build_web_toolset`; the returned
:class:`InternalToolsetProvider` exposes three tools:

* ``web_search`` — dispatches through a
  :class:`~primer.web_search.service.WebSearchService` (which consults
  the active-config singleton and routes to the appropriate adapter
  via the :class:`WebSearchRegistry`).
* ``web_fetch`` - dispatches through a
  :class:`~primer.web_fetch.service.WebFetchService` to fetch a URL and
  return clean markdown of the page's main content.
* ``http_request`` — backed by :class:`httpx.AsyncClient`.

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

from typing import TYPE_CHECKING

import httpx

from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.web.tools import (
    HttpMethod,
    HttpRequestArgs,
    WebFetchArgs,
    WebSearchArgs,
    make_http_request_descriptor,
    make_http_request_handler,
    make_web_fetch_descriptor,
    make_web_fetch_handler,
    make_web_search_descriptor,
    make_web_search_handler,
)


if TYPE_CHECKING:
    from primer.web_fetch.service import WebFetchService
    from primer.web_search.service import WebSearchService


_DEFAULT_RESPONSE_BODY_BYTE_CAP = 1_000_000  # 1 MB


def build_web_toolset(
    *,
    web_search_service: "WebSearchService",
    web_fetch_service: "WebFetchService",
    toolset_id: str = "web",
    http_client: httpx.AsyncClient | None = None,
    response_body_byte_cap: int = _DEFAULT_RESPONSE_BODY_BYTE_CAP,
) -> InternalToolsetProvider:
    """Construct the always-on ``web`` toolset.

    Wires the web_search tool to the supplied WebSearchService (which
    itself routes through the active-config singleton); the http_request
    tool to a long-lived :class:`httpx.AsyncClient`.

    Callers MUST supply ``web_search_service``. The previous ``backend``
    kwarg with default-None behaviour is removed: there's no in-process
    construction of a backend any more; the registry + service own all
    adapter lifecycles.

    Parameters
    ----------
    web_search_service
        :class:`~primer.web_search.service.WebSearchService` the
        ``web_search`` handler delegates to. Required.
    toolset_id
        Wire id stamped onto every :class:`Tool` descriptor and used by
        the :class:`ToolExecutionManager` to route calls back here.
        Defaults to ``"web"``.
    http_client
        Optional :class:`httpx.AsyncClient` used by the ``http_request``
        tool. When ``None``, the factory constructs a default async
        client. Callers running long-lived applications should pass a
        shared client and manage its lifecycle (``await client.aclose()``).
    response_body_byte_cap
        Maximum bytes returned in ``http_request`` response bodies;
        anything past the cap is truncated and ``"truncated": true``
        is set in the result. Defaults to 1 MB.

    Returns
    -------
    InternalToolsetProvider
        A ready-to-register provider with ``web_search`` and
        ``http_request`` tools wired in.
    """
    chosen_client: httpx.AsyncClient = (
        http_client if http_client is not None else httpx.AsyncClient(timeout=30.0)
    )

    registry: dict[str, tuple] = {
        "web_search": (
            make_web_search_descriptor(toolset_id),
            make_web_search_handler(web_search_service),
        ),
        "web_fetch": (
            make_web_fetch_descriptor(toolset_id),
            make_web_fetch_handler(web_fetch_service),
        ),
        "http_request": (
            make_http_request_descriptor(toolset_id),
            make_http_request_handler(
                http_client=chosen_client,
                response_body_byte_cap=response_body_byte_cap,
            ),
        ),
    }
    return InternalToolsetProvider(toolset_id, registry)


__all__ = [
    "HttpMethod",
    "HttpRequestArgs",
    "WebFetchArgs",
    "WebSearchArgs",
    "build_web_toolset",
]
