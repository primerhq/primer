"""Per-row SemanticSearchProvider registry.

Caches one ``VectorStoreProvider`` instance per ``SemanticSearchProvider``
row id, lazy-constructs from the row config, invalidates on demand.

Mirrors :class:`primer.api.registries.provider_registry.ProviderRegistry`'s
caching pattern for LLM/Embedding/CrossEncoder providers — same
``get`` / ``invalidate`` / ``aclose`` triad, scoped per id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.api.registries._invalidation import BusInvalidated
from primer.api.registries.retirement import RETIRE_IDLE_SECONDS, AdapterRetirement
from primer.int.coordinator import InvalidationTopic

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


class SemanticSearchRegistry(BusInvalidated):
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
        retire_idle_seconds: float = RETIRE_IDLE_SECONDS,
    ) -> None:
        self._retirement = AdapterRetirement(retire_idle_seconds)
        self._init_invalidation(InvalidationTopic.SEMANTIC_SEARCH_PROVIDER)
        self._storage = storage
        self._factory = factory or _default_factory
        self._instances: dict[str, "VectorStoreProvider"] = {}
        self._lock = asyncio.Lock()
        # Bumped by every eviction and flush: a get that read its row before one never caches what it built (GX-4).
        self._generation = 0

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
        generation = self._generation
        row = await self._storage.get(ssp_id)
        if row is None:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(
                f"semantic search provider {ssp_id!r} does not exist"
            )
        provider = self._retirement.track(self._factory(row))
        await provider.initialize()

        # Insert under the lock, with a re-check in case a concurrent
        # caller beat us; an invalidate() in the meantime moved the
        # generation, and then the instance is this caller's only.
        async with self._lock:
            winner = self._instances.get(ssp_id)
            if winner is not None:
                # Concurrent get won the race; close our duplicate and
                # return the cached winner.
                close_loser = provider
                provider = winner
            else:
                if self._generation == generation:
                    self._instances[ssp_id] = provider
                else:
                    self._retirement.retire(provider, repr(ssp_id))
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

    async def _invalidate_local(self, ssp_id: str) -> None:
        """Drop the cached instance for one id (this process); retire it.

        Called AFTER the provider row was updated or deleted, so a failing close is logged and swallowed, like every
        other close in this registry: raising would report a write that already landed as failed. The instance is
        already out of the cache, so the next ``get_provider`` rebuilds it either way.
        """
        async with self._lock:
            self._generation += 1
            inst = self._instances.pop(ssp_id, None)
        if inst is not None:
            self._retirement.retire(inst, repr(ssp_id))         # closed once its in-flight calls are done (GX-1)

    def _flush_local(self) -> None:
        """A bus reconnect may have dropped evictions: retire every cached adapter (in-flight calls finish), so the next get rebuilds."""
        self._generation += 1
        instances, self._instances = list(self._instances.values()), {}
        for inst in instances:
            self._retirement.retire(inst)

    async def aclose(self) -> None:
        """Unsubscribe, then drop + aclose every cached instance."""
        await self._unbind_invalidation_bus()
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
        await self._retirement.close_all()
