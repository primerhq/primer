"""Per-row SemanticSearchProvider registry.

Caches one ``VectorStoreProvider`` instance per ``SemanticSearchProvider``
row id, lazy-constructs from the row config, invalidates on demand.

Mirrors :class:`matrix.api.registries.provider_registry.ProviderRegistry`'s
caching pattern for LLM/Embedding/CrossEncoder providers — same
``get`` / ``invalidate`` / ``aclose`` triad, scoped per id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.storage import Storage
    from primer.int.vector_store import VectorStore
    from primer.int.vector_store_provider import VectorStoreProvider
    from primer.model.provider import SemanticSearchProvider


logger = logging.getLogger(__name__)


def _default_factory(row: "SemanticSearchProvider") -> "VectorStoreProvider":  # pragma: no cover
    """Dispatch a SemanticSearchProvider row to its concrete backend.

    Adapts the SSP row into the existing VectorStoreProviderConfig
    shape (the two type families share the same backend enum values
    and the same config classes), so the existing
    VectorStoreProviderFactory can dispatch without modification.
    """
    # NOTE: VectorStoreProviderConfig / VectorStoreProviderType are internal
    # adapter shapes (not public API). They bridge the SSP row to the
    # existing VectorStoreProviderFactory dispatch.
    from primer.model.provider import (
        VectorStoreProviderConfig,
        VectorStoreProviderType,
    )
    from primer.vector.factory import VectorStoreProviderFactory

    config = VectorStoreProviderConfig(
        provider=VectorStoreProviderType(row.provider.value),
        config=row.config,
    )
    return VectorStoreProviderFactory.create(config)


class SemanticSearchRegistry:
    """Cache + lifecycle for per-row SemanticSearchProvider instances.

    ``get_provider(id)`` lazy-resolves the row from Storage, dispatches
    to the configured backend factory, and caches the resulting
    ``VectorStoreProvider``. ``invalidate(id)`` drops the cache entry
    and calls ``aclose()`` on the instance.
    """

    def __init__(
        self,
        *,
        storage: "Storage",
        factory: Callable[["SemanticSearchProvider"], "VectorStoreProvider"] | None = None,
    ) -> None:
        self._storage = storage
        self._factory = factory or _default_factory
        self._instances: dict[str, "VectorStoreProvider"] = {}
        self._lock = asyncio.Lock()

    async def get_provider(self, ssp_id: str) -> "VectorStoreProvider":
        """Resolve a row to its live VectorStoreProvider instance.

        Cached per id. Slow I/O (storage lookup, factory invocation,
        initialize) runs OUTSIDE ``self._lock`` so concurrent calls for
        different ids don't serialise on each other. Concurrent calls
        for the SAME id may construct twice but only one wins the
        cache; the loser is aclose()'d to avoid leaking resources.
        """
        # Fast path: cache hit.
        async with self._lock:
            cached = self._instances.get(ssp_id)
        if cached is not None:
            return cached

        # Slow path: resolve + construct + initialize OUTSIDE the lock.
        row = await self._storage.get(ssp_id)
        provider = self._factory(row)
        await provider.initialize()

        # Insert under the lock, with a re-check in case a concurrent
        # caller beat us (or invalidate() ran in the meantime).
        async with self._lock:
            winner = self._instances.get(ssp_id)
            if winner is not None:
                # Concurrent get won the race; close our duplicate and
                # return the cached winner.
                close_loser = provider
                provider = winner
            else:
                self._instances[ssp_id] = provider
                close_loser = None
        if close_loser is not None:
            try:
                await close_loser.aclose()
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "SemanticSearchRegistry: aclose() on race-loser instance for "
                    "%r failed: %s", ssp_id, close_loser,
                )
        return provider

    async def get_store(self, ssp_id: str) -> "VectorStore":
        """Convenience: resolve provider + return its VectorStore. Use when only the VectorStore interface is needed and provider lifecycle is managed elsewhere."""
        p = await self.get_provider(ssp_id)
        return p.get_vector_store()

    async def invalidate(self, ssp_id: str) -> None:
        """Drop the cached instance for one id; aclose() it."""
        async with self._lock:
            inst = self._instances.pop(ssp_id, None)
        if inst is not None:
            await inst.aclose()

    async def aclose(self) -> None:
        """Drop + aclose every cached instance."""
        async with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for inst in instances:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "SemanticSearchRegistry.aclose: instance close failed: %s",
                    exc,
                )
