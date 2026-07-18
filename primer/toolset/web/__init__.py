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
    DownloadArgs,
    HttpMethod,
    HttpRequestArgs,
    WebFetchArgs,
    WebSearchArgs,
    make_download_descriptor,
    make_download_handler,
    make_http_request_descriptor,
    make_http_request_handler,
    make_web_fetch_descriptor,
    make_web_fetch_handler,
    make_web_search_descriptor,
    make_web_search_handler,
)


if TYPE_CHECKING:
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.web_fetch.service import WebFetchService
    from primer.web_search.service import WebSearchService


_DEFAULT_RESPONSE_BODY_BYTE_CAP = 1_000_000  # 1 MB
_DEFAULT_DOWNLOAD_BYTE_CAP = 100_000_000  # 100 MB


def build_web_toolset(
    *,
    web_search_service: "WebSearchService",
    web_fetch_service: "WebFetchService",
    toolset_id: str = "web",
    http_client: httpx.AsyncClient | None = None,
    response_body_byte_cap: int = _DEFAULT_RESPONSE_BODY_BYTE_CAP,
    workspace_registry: "WorkspaceRegistry | None" = None,
    download_byte_cap: int = _DEFAULT_DOWNLOAD_BYTE_CAP,
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
    workspace_registry
        Optional :class:`WorkspaceRegistry`. When supplied, the
        workspace-only ``download`` tool is registered (it resolves the
        live workspace from ``ctx.workspace_id`` and writes the fetched
        bytes into it). When ``None`` (e.g. bare/test builds with no
        workspace layer), ``download`` is omitted and the toolset keeps
        its three stateless tools.
    download_byte_cap
        Default hard cap on ``download`` size in bytes; a stream that
        exceeds it is rejected (nothing is written). A per-call
        ``max_bytes`` overrides it downward. Defaults to 100 MB.

    Returns
    -------
    InternalToolsetProvider
        A ready-to-register provider with ``web_search``, ``web_fetch``,
        and ``http_request`` tools wired in (plus ``download`` when a
        ``workspace_registry`` is supplied).
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
    # Workspace-only download tool: only wired when the workspace layer is
    # available. requires_workspace=True keeps it out of chat context and
    # off the MCP surface.
    if workspace_registry is not None:
        registry["download"] = (
            make_download_descriptor(toolset_id),
            make_download_handler(
                http_client=chosen_client,
                workspace_registry=workspace_registry,
                byte_cap=download_byte_cap,
            ),
        )
    return InternalToolsetProvider(toolset_id, registry)


__all__ = [
    "DownloadArgs",
    "HttpMethod",
    "HttpRequestArgs",
    "WebFetchArgs",
    "WebSearchArgs",
    "build_web_toolset",
]
