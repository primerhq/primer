"""Jina Reader adapter: GET https://r.jina.ai/<url> -> markdown."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from primer.web_fetch.adapter import (
    FetchedPage, WebFetchAdapter, WebFetchProviderError, WebFetchUnavailable,
)

if TYPE_CHECKING:
    from primer.model.web_fetch import JinaFetchConfig

logger = logging.getLogger(__name__)
JINA_BASE_URL = "https://r.jina.ai"


class JinaAdapter(WebFetchAdapter):
    def __init__(self, config: "JinaFetchConfig", *, client: httpx.AsyncClient | None = None,
                 base_url: str = JINA_BASE_URL) -> None:
        self._api_key = config.api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def fetch(self, *, url: str) -> FetchedPage:
        headers = {"Accept": "text/markdown"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        try:
            r = await self._client.get(f"{self._base_url}/{url}", headers=headers)
        except httpx.HTTPError as exc:
            raise WebFetchUnavailable(f"jina transport: {type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403):
            raise WebFetchProviderError(f"jina auth failed (HTTP {r.status_code})")
        if r.status_code == 429:
            raise WebFetchUnavailable("jina rate-limited (HTTP 429)")
        if r.status_code >= 500:
            raise WebFetchUnavailable(f"jina server error (HTTP {r.status_code})")
        if r.status_code != 200:
            raise WebFetchProviderError(f"jina unexpected status {r.status_code}")
        return FetchedPage(
            final_url=url, title="", content_markdown=r.text,
            content_type="text/markdown", status=r.status_code,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["JINA_BASE_URL", "JinaAdapter"]
