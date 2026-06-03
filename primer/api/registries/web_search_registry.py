"""Per-row WebSearchProvider registry.

Caches one ``WebSearchAdapter`` instance per ``WebSearchProvider`` row
id, lazy-constructs from the row config, invalidates on demand.

Mirrors :class:`primer.api.registries.semantic_search_registry.SemanticSearchRegistry`
exactly: same get / invalidate / aclose triad scoped per id; same
race-resilience pattern (concurrent gets for one id may construct
twice but only one wins the cache; the loser is aclose()'d).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.model.except_ import NotFoundError
from primer.model.web_search import (
    WebSearchProvider,
    WebSearchProviderType,
)
from primer.web_search.adapter import WebSearchAdapter


if TYPE_CHECKING:
    from primer.int.storage import Storage


logger = logging.getLogger(__name__)


class WebSearchRegistry:
    """Cache + lifecycle for per-row WebSearchAdapter instances."""

    def __init__(
        self,
        *,
        storage: "Storage[WebSearchProvider]",
        factory: Callable[[WebSearchProvider], WebSearchAdapter] | None = None,
    ) -> None:
        self._storage = storage
        self._factory = factory or default_web_search_factory
        self._instances: dict[str, WebSearchAdapter] = {}
        self._lock = asyncio.Lock()

    async def get(self, provider_id: str) -> WebSearchAdapter:
        """Resolve a row to its live adapter instance.

        Cached per id. Slow I/O (storage lookup, factory call) runs
        OUTSIDE ``self._lock`` so concurrent calls for different ids
        don't serialise. Concurrent calls for the SAME id may
        construct twice but only one wins the cache; the loser is
        aclose()'d to avoid leaking resources.
        """
        # Fast path: cache hit under the lock.
        async with self._lock:
            cached = self._instances.get(provider_id)
            if cached is not None:
                return cached

        # Slow path: storage + construct OUTSIDE the lock.
        row = await self._storage.get(provider_id)
        if row is None:
            raise NotFoundError(
                f"web search provider {provider_id!r} does not exist"
            )
        candidate = self._factory(row)

        # Insert under the lock with a re-check.
        async with self._lock:
            winner = self._instances.get(provider_id)
            if winner is None:
                self._instances[provider_id] = candidate
                return candidate

        # Race-loser path: close our candidate, return the winner.
        try:
            await candidate.aclose()
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.warning(
                "WebSearchRegistry: race-loser aclose failed: %s", exc,
            )
        return winner

    async def invalidate(self, provider_id: str) -> None:
        """Drop the cached instance for one id; aclose() it."""
        async with self._lock:
            inst = self._instances.pop(provider_id, None)
        if inst is not None:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WebSearchRegistry.invalidate: aclose failed: %s", exc,
                )

    async def aclose(self) -> None:
        """Drop + aclose every cached instance."""
        async with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for inst in instances:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WebSearchRegistry.aclose: instance close failed: %s",
                    exc,
                )


def default_web_search_factory(
    provider: WebSearchProvider,
) -> WebSearchAdapter:
    """Construct the right adapter for the provider's type.

    Lazy imports keep the optional API-key-bearing adapters (Tavily,
    Firecrawl, Exa) out of the import graph for installs that don't
    use them.
    """
    match provider.provider_type:
        case WebSearchProviderType.DUCKDUCKGO:
            from primer.web_search.duckduckgo import DuckDuckGoAdapter
            return DuckDuckGoAdapter(provider.config)
        case WebSearchProviderType.TAVILY:
            from primer.web_search.tavily import TavilyAdapter
            return TavilyAdapter(provider.config)
        case WebSearchProviderType.FIRECRAWL:
            from primer.web_search.firecrawl import FirecrawlAdapter
            return FirecrawlAdapter(provider.config)
        case WebSearchProviderType.EXA:
            from primer.web_search.exa import ExaAdapter
            return ExaAdapter(provider.config)


__all__ = ["WebSearchRegistry", "default_web_search_factory"]
