"""Firecrawl /v1/scrape adapter (onlyMainContent markdown; renders JS)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from primer.common.transport_text import require_sendable_key, transport_failure, unexpected_status
from primer.web_fetch.adapter import (
    FetchedPage, WebFetchAdapter, WebFetchProviderError, WebFetchUnavailable,
)

if TYPE_CHECKING:
    from primer.model.web_fetch import FirecrawlFetchConfig

logger = logging.getLogger(__name__)
FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"


class FirecrawlAdapter(WebFetchAdapter):
    def __init__(self, config: "FirecrawlFetchConfig", *, client: httpx.AsyncClient | None = None,
                 base_url: str = FIRECRAWL_BASE_URL) -> None:
        self._config = config
        self._api_key = config.api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._owns_client = client is None

    async def fetch(self, *, url: str) -> FetchedPage:
        body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
        require_sendable_key("firecrawl", self._api_key, WebFetchProviderError)          # a key h11 would refuse is not sent: no request, no span
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        try:
            r = await self._client.post(f"{self._base_url}/v1/scrape", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WebFetchUnavailable(transport_failure("firecrawl", exc, self._config)) from None
        if r.status_code in (401, 402, 403):
            raise WebFetchProviderError(f"firecrawl auth/quota failed (HTTP {r.status_code})")
        if r.status_code == 429:
            raise WebFetchUnavailable("firecrawl rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebFetchUnavailable(f"firecrawl server error (HTTP {r.status_code})")
        if r.status_code != 200:
            raise WebFetchProviderError(unexpected_status("firecrawl", r.status_code, r.text, self._config))
        try:
            data = r.json()
        except ValueError as exc:
            raise WebFetchProviderError(f"firecrawl returned non-JSON: {exc}") from exc
        d = data.get("data") or {}
        meta = d.get("metadata") or {}
        return FetchedPage(
            final_url=meta.get("sourceURL") or url,
            title=meta.get("title") or "",
            content_markdown=d.get("markdown") or "",
            content_type="text/markdown",
            status=r.status_code,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["FIRECRAWL_BASE_URL", "FirecrawlAdapter"]
