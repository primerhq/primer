"""Per-row WebFetchProvider registry. Mirrors WebSearchRegistry exactly:
same get / invalidate / aclose triad + race-resilience pattern."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.model.except_ import NotFoundError
from primer.model.web_fetch import WebFetchProvider, WebFetchProviderType
from primer.web_fetch.adapter import WebFetchAdapter

if TYPE_CHECKING:
    from primer.int.storage import Storage

logger = logging.getLogger(__name__)


class WebFetchRegistry:
    def __init__(self, *, storage: "Storage[WebFetchProvider]",
                 factory: Callable[[WebFetchProvider], WebFetchAdapter] | None = None) -> None:
        self._storage = storage
        self._factory = factory or default_web_fetch_factory
        self._instances: dict[str, WebFetchAdapter] = {}
        self._lock = asyncio.Lock()

    async def get(self, provider_id: str) -> WebFetchAdapter:
        async with self._lock:
            cached = self._instances.get(provider_id)
            if cached is not None:
                return cached
        row = await self._storage.get(provider_id)
        if row is None:
            raise NotFoundError(f"web fetch provider {provider_id!r} does not exist")
        candidate = self._factory(row)
        async with self._lock:
            winner = self._instances.get(provider_id)
            if winner is None:
                self._instances[provider_id] = candidate
                return candidate
        try:
            await candidate.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebFetchRegistry: race-loser aclose failed: %s", exc)
        return winner

    async def invalidate(self, provider_id: str) -> None:
        async with self._lock:
            inst = self._instances.pop(provider_id, None)
        if inst is not None:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebFetchRegistry.invalidate: aclose failed: %s", exc)

    async def aclose(self) -> None:
        async with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for inst in instances:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebFetchRegistry.aclose: instance close failed: %s", exc)


def default_web_fetch_factory(provider: WebFetchProvider) -> WebFetchAdapter:
    match provider.provider_type:
        case WebFetchProviderType.LOCAL:
            from primer.web_fetch.local import LocalAdapter
            return LocalAdapter()
        case WebFetchProviderType.JINA:
            from primer.web_fetch.jina import JinaAdapter
            return JinaAdapter(provider.config)
        case WebFetchProviderType.FIRECRAWL:
            from primer.web_fetch.firecrawl import FirecrawlAdapter
            return FirecrawlAdapter(provider.config)
        case WebFetchProviderType.EXA:
            from primer.web_fetch.exa import ExaAdapter
            return ExaAdapter(provider.config)


__all__ = ["WebFetchRegistry", "default_web_fetch_factory"]
