"""Firecrawl-backed WebSearchAdapter.

Wraps Firecrawl's REST ``POST /v1/search`` endpoint over
httpx.AsyncClient.

Error mapping (mirrors the Tavily adapter's table):

================  ========================================
Firecrawl resp.   Adapter raises
================  ========================================
200 + valid JSON  returns list[SearchHit]
401 / 403         WebSearchProviderError("firecrawl auth failed")
402               WebSearchProviderError("firecrawl payment required")
429               WebSearchUnavailable("firecrawl rate-limited")
5xx               WebSearchUnavailable("firecrawl server error")
other non-200     WebSearchProviderError("firecrawl unexpected status")
transport error   WebSearchUnavailable("firecrawl transport")
non-JSON body     WebSearchProviderError("firecrawl returned non-JSON")
================  ========================================

Firecrawl does not surface a safe_search parameter. The tool's
three-tier enum is preserved at the tool boundary; this adapter
ignores the level (logs at DEBUG so operators can see the value
that was discarded) and the underlying engine's default applies.

The Firecrawl response shape is
``{"success": true, "data": [{"url", "title", "description", ...}, ...]}``.
``description`` maps to :attr:`SearchHit.snippet`. Pre-scraped
``markdown`` content is ignored — the wire contract for this tool is
title/url/snippet, not full page content.
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
    from primer.model.web_search import FirecrawlConfig


logger = logging.getLogger(__name__)


FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"


class FirecrawlAdapter(WebSearchAdapter):
    """WebSearchAdapter implementation backed by Firecrawl's REST API."""

    def __init__(
        self,
        config: "FirecrawlConfig",
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = FIRECRAWL_BASE_URL,
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
        if safe_search != "moderate":
            logger.debug(
                "firecrawl: ignoring safe_search=%r (not supported by the API)",
                safe_search,
            )
        body = {
            "query": query,
            "limit": count,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
        }

        try:
            r = await self._client.post(
                f"{self._base_url}/v1/search",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise WebSearchUnavailable(
                f"firecrawl transport: {type(exc).__name__}: {exc}"
            ) from exc

        if r.status_code in (401, 403):
            raise WebSearchProviderError(
                f"firecrawl auth failed (HTTP {r.status_code}): check api_key"
            )
        if r.status_code == 402:
            raise WebSearchProviderError(
                "firecrawl payment required (HTTP 402): top up your account"
            )
        if r.status_code == 429:
            raise WebSearchUnavailable("firecrawl rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebSearchUnavailable(
                f"firecrawl server error (HTTP {r.status_code})"
            )
        if r.status_code != 200:
            raise WebSearchProviderError(
                f"firecrawl unexpected status {r.status_code}: {r.text[:200]}"
            )

        try:
            data = r.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                f"firecrawl returned non-JSON: {exc}"
            ) from exc

        # Firecrawl wraps success/failure into a top-level flag; an
        # explicit success=false on a 200 is still an error.
        if data.get("success") is False:
            error_msg = data.get("error") or "(no error message)"
            raise WebSearchProviderError(
                f"firecrawl reported failure: {error_msg}"
            )

        results = data.get("data", []) or []
        return [
            SearchHit(
                title=(item.get("title") or item.get("url", "")),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
            )
            for item in results[:count]
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["FIRECRAWL_BASE_URL", "FirecrawlAdapter"]
