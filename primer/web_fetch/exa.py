"""Exa /contents adapter (returns page text)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from primer.web_fetch.adapter import (
    FetchedPage, WebFetchAdapter, WebFetchProviderError, WebFetchUnavailable,
)

if TYPE_CHECKING:
    from primer.model.web_fetch import ExaFetchConfig

logger = logging.getLogger(__name__)
EXA_BASE_URL = "https://api.exa.ai"


class ExaAdapter(WebFetchAdapter):
    def __init__(self, config: "ExaFetchConfig", *, client: httpx.AsyncClient | None = None,
                 base_url: str = EXA_BASE_URL) -> None:
        self._api_key = config.api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def fetch(self, *, url: str) -> FetchedPage:
        body = {"ids": [url], "text": True}
        headers = {"x-api-key": self._api_key.get_secret_value()}
        try:
            r = await self._client.post(f"{self._base_url}/contents", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WebFetchUnavailable(f"exa transport: {type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403):
            raise WebFetchProviderError(f"exa auth failed (HTTP {r.status_code})")
        if r.status_code == 429:
            raise WebFetchUnavailable("exa rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebFetchUnavailable(f"exa server error (HTTP {r.status_code})")
        if r.status_code != 200:
            raise WebFetchProviderError(f"exa unexpected status {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise WebFetchProviderError(f"exa returned non-JSON: {exc}") from exc
        results = data.get("results") or []
        if not results:
            return FetchedPage(final_url=url, title="", content_markdown="",
                               content_type="text/plain", status=r.status_code, is_thin=True)
        item = results[0]
        return FetchedPage(
            final_url=item.get("url") or url,
            title=item.get("title") or "",
            content_markdown=item.get("text") or "",
            content_type="text/plain",
            status=r.status_code,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["EXA_BASE_URL", "ExaAdapter"]
