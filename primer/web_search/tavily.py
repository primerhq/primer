"""Tavily-backed WebSearchAdapter.

Wraps Tavily's REST ``POST /search`` endpoint over httpx.AsyncClient.

Error mapping (see spec §5.3 for the full table):

================  ========================================
Tavily response   Adapter raises
================  ========================================
200 + valid JSON  returns list[SearchHit]
401 / 403         WebSearchProviderError("tavily auth failed")
429               WebSearchUnavailable("tavily rate-limited")
5xx               WebSearchUnavailable("tavily server error")
other non-200     WebSearchProviderError("tavily unexpected status")
transport error   WebSearchUnavailable("tavily transport")
non-JSON body     WebSearchProviderError("tavily returned non-JSON")
================  ========================================

Safe-search mapping is lossy: Tavily exposes a boolean, so
``off → false`` and ``moderate / strict → true``. The tool's
three-tier enum is preserved at the tool boundary; the collapse
happens inside this adapter.

The Tavily response shape is
``{"results": [{"title", "url", "content", ...}, ...]}``. The
``content`` field maps to ``SearchHit.snippet``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from primer.web_search.adapter import (
    SafeSearchLevel,
    SearchHit,
    WebSearchAdapter,
    WebSearchProviderError,
    WebSearchUnavailable,
)


if TYPE_CHECKING:
    from primer.model.web_search import TavilyConfig


logger = logging.getLogger(__name__)


TAVILY_BASE_URL = "https://api.tavily.com"


class TavilyAdapter(WebSearchAdapter):
    """WebSearchAdapter implementation backed by Tavily's REST API."""

    def __init__(
        self,
        config: "TavilyConfig",
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = TAVILY_BASE_URL,
    ) -> None:
        self._api_key = config.api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        body = {
            "api_key": self._api_key.get_secret_value(),
            "query": query,
            "max_results": count,
            "include_answer": False,
            "search_depth": "basic",
            "include_raw_content": False,
            # Lossy collapse: Tavily exposes a boolean only.
            "safe_search": safe_search != "off",
        }

        try:
            r = await self._client.post(
                f"{self._base_url}/search", json=body,
            )
        except httpx.HTTPError as exc:
            raise WebSearchUnavailable(
                f"tavily transport: {type(exc).__name__}: {exc}"
            ) from exc

        if r.status_code in (401, 403):
            raise WebSearchProviderError(
                f"tavily auth failed (HTTP {r.status_code}): check api_key"
            )
        if r.status_code == 429:
            raise WebSearchUnavailable("tavily rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebSearchUnavailable(
                f"tavily server error (HTTP {r.status_code})"
            )
        if r.status_code != 200:
            raise WebSearchProviderError(
                f"tavily unexpected status {r.status_code}: {r.text[:200]}"
            )

        try:
            data = r.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                f"tavily returned non-JSON: {exc}"
            ) from exc

        results = data.get("results", []) or []
        return [
            SearchHit(
                title=(item.get("title") or item.get("url", "")),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            )
            for item in results[:count]
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["TAVILY_BASE_URL", "TavilyAdapter"]
