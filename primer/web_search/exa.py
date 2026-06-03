"""Exa-backed WebSearchAdapter.

Wraps Exa's REST ``POST /search`` endpoint over httpx.AsyncClient.

Error mapping (mirrors the Tavily / Firecrawl adapters' tables):

================  ========================================
Exa response      Adapter raises
================  ========================================
200 + valid JSON  returns list[SearchHit]
401 / 403         WebSearchProviderError("exa auth failed")
429               WebSearchUnavailable("exa rate-limited")
5xx               WebSearchUnavailable("exa server error")
other non-200     WebSearchProviderError("exa unexpected status")
transport error   WebSearchUnavailable("exa transport")
non-JSON body     WebSearchProviderError("exa returned non-JSON")
================  ========================================

Exa authenticates via the ``x-api-key`` header (not bearer auth like
Tavily and Firecrawl).

Exa has no ``safe_search`` parameter. The tool's three-tier enum is
preserved at the tool boundary; this adapter ignores the level
(DEBUG-logs the discarded value).

To get a snippet for each hit, the adapter requests text content via
the ``contents.text`` flag — Exa otherwise returns title + url only.
The text is truncated by Exa to a sane preview length by default; we
do not request the full document. The mapping is:

  title     <- item.title (falls back to item.url)
  url       <- item.url
  snippet   <- item.text (empty if Exa returned none)

Exa's ``type`` field controls retrieval mode (``auto``, ``neural``,
``keyword``). ``auto`` is the default and what we use — Exa picks the
right strategy per query.
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
    from primer.model.web_search import ExaConfig


logger = logging.getLogger(__name__)


EXA_BASE_URL = "https://api.exa.ai"


class ExaAdapter(WebSearchAdapter):
    """WebSearchAdapter implementation backed by Exa's REST API."""

    def __init__(
        self,
        config: "ExaConfig",
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = EXA_BASE_URL,
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
                "exa: ignoring safe_search=%r (not supported by the API)",
                safe_search,
            )
        body = {
            "query": query,
            "numResults": count,
            "type": "auto",
            # Ask for short text previews so we have something to put in
            # SearchHit.snippet. Without this, Exa returns only title +
            # url and the snippet would always be empty.
            "contents": {"text": True},
        }
        headers = {
            "x-api-key": self._api_key.get_secret_value(),
        }

        try:
            r = await self._client.post(
                f"{self._base_url}/search",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise WebSearchUnavailable(
                f"exa transport: {type(exc).__name__}: {exc}"
            ) from exc

        if r.status_code in (401, 403):
            raise WebSearchProviderError(
                f"exa auth failed (HTTP {r.status_code}): check api_key"
            )
        if r.status_code == 429:
            raise WebSearchUnavailable("exa rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebSearchUnavailable(
                f"exa server error (HTTP {r.status_code})"
            )
        if r.status_code != 200:
            raise WebSearchProviderError(
                f"exa unexpected status {r.status_code}: {r.text[:200]}"
            )

        try:
            data = r.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                f"exa returned non-JSON: {exc}"
            ) from exc

        results = data.get("results", []) or []
        return [
            SearchHit(
                title=(item.get("title") or item.get("url", "")),
                url=item.get("url", ""),
                snippet=item.get("text", ""),
            )
            for item in results[:count]
        ]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["EXA_BASE_URL", "ExaAdapter"]
