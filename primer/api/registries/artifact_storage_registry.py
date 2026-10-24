"""Per-row ArtifactStorageProvider registry.

Caches one live :class:`primer.int.artifact_storage.ArtifactStorage` per
``ArtifactStorageProvider`` row id, lazy-constructs from the row config,
invalidates on demand. Mirrors
:class:`primer.api.registries.semantic_search_registry.SemanticSearchRegistry`.

:meth:`ArtifactStorageRegistry.get_default` is the one entry point the artifact
consumers use. It returns the routing store
(:class:`primer.artifact.routing.RoutingArtifactStorage`), which writes to the
active provider and finds an artifact again through its location record; the
provider instances it routes to are the ones this registry caches.

Every process has its own registry and so its own cache. :meth:`ArtifactStorageRegistry.bind_invalidation_bus` ties them together: an edit or a delete of a
provider row, through the REST route or a system tool, publishes on the ``artifact_storage_provider`` topic (ticket 01a11358, the artifact half) and every
process, the publisher included, drops its own copy from its subscription. The active-store setting is not cached anywhere (the routing store reads it on
every put), so the topic carries provider-row evictions only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.artifact.routing import RoutingArtifactStorage
from primer.int.coordinator import InvalidationTopic

# Reserved id of the auto-seeded default provider (the DB backend), so chat
# media works with zero operator configuration. Re-exported: it is defined in
# the leaf ``primer.model.providers.artifact`` (and re-exported by
# ``primer.artifact.factory``) because ``primer.artifact`` must not import
# ``primer.api`` (its check is imported by the system toolset, which
# ``primer.api`` imports).
from primer.artifact.factory import DEFAULT_ARTIFACT_PROVIDER_ID

if TYPE_CHECKING:
    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.coordinator import InvalidationBus, InvalidationSubscription
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider
    from primer.model.provider import ArtifactStorageProvider


logger = logging.getLogger(__name__)


class ArtifactStorageRegistry:
    """Cache + lifecycle for per-row ArtifactStorage instances."""

    def __init__(
        self,
        *,
        storage: "Storage",
        storage_provider: "StorageProvider",
        default_provider_id: str = DEFAULT_ARTIFACT_PROVIDER_ID,
        factory: "Callable[[ArtifactStorageProvider, StorageProvider], ArtifactStorage] | None" = None,
    ) -> None:
        self._storage = storage
        self._storage_provider = storage_provider
        self._default_provider_id = default_provider_id
        self._factory = factory or _default_factory
        self._instances: dict[str, "ArtifactStorage"] = {}
        self._lock = asyncio.Lock()
        self._routing: RoutingArtifactStorage | None = None
        # Bumped by every eviction and flush. ``get_provider`` samples it before it reads the row and refuses to cache the instance it built if the value
        # moved while it was suspended: that instance came from a row read BEFORE the eviction, so caching it would keep exactly the staleness the
        # eviction exists to clear (the same guard as ``ProviderRegistry._cache_generation``).
        self._generation = 0
        self._invalidation_bus: "InvalidationBus | None" = None
        self._invalidation_subs: list["InvalidationSubscription"] = []

    async def get_provider(self, provider_id: str) -> "ArtifactStorage":
        """Resolve a row to its live ArtifactStorage instance (cached per id)."""
        async with self._lock:
            cached = self._instances.get(provider_id)
        if cached is not None:
            return cached

        generation = self._generation
        row = await self._storage.get(provider_id)
        if row is None:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(
                f"ArtifactStorageProvider {provider_id!r} does not exist"
            )
        provider = self._factory(row, self._storage_provider)
        await provider.initialize()

        async with self._lock:
            winner = self._instances.get(provider_id)
            if winner is not None:
                close_loser = provider
                provider = winner
            else:
                close_loser = None
                # An eviction or a flush landed while this instance was being built. The caller still gets it (its request completes), but the next get
                # builds a fresh one from the current row. It is not closed here: the caller is about to use it.
                if self._generation == generation:
                    self._instances[provider_id] = provider
        if close_loser is not None:
            try:
                await close_loser.aclose()
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "ArtifactStorageRegistry: aclose() on race-loser for %r failed",
                    provider_id,
                )
        return provider

    async def get_default(self) -> RoutingArtifactStorage:
        """The store every artifact consumer uses: one routing store, built on first use and then returned every time.

        It routes a put to the active provider (resolved through :meth:`get_provider`) and a get or a delete to the provider recorded for the
        artifact; an id with no record is read from the database directly. It needs no provider row to exist at construction, builds no provider
        instance, and holds nothing that an invalidation or a flush would have to drop.
        """
        if self._routing is None:
            from primer.artifact.db import DbArtifactStorage
            from primer.model.artifact import ArtifactLocation
            from primer.model.providers.artifact import ActiveArtifactStorageConfig

            self._routing = RoutingArtifactStorage(
                resolve=self.get_provider,
                fallback=DbArtifactStorage(self._storage_provider),
                locations=self._storage_provider.get_storage(ArtifactLocation),
                active_config=self._storage_provider.get_storage(ActiveArtifactStorageConfig),
                providers=self._storage,
                default_provider_id=self._default_provider_id,
            )
        return self._routing

    async def invalidate(self, provider_id: str) -> None:
        """Drop the cached store of one provider row in every process.

        With the invalidation bus bound this publishes, and each process (this one included) evicts its own copy from its subscription. With no bus
        (a single process whose lifespan did not bind one, a standalone builder) it evicts here.
        """
        if self._invalidation_bus is not None:
            await self._invalidation_bus.publish(InvalidationTopic.ARTIFACT_STORAGE_PROVIDER, provider_id)
        else:
            await self._invalidate_local(provider_id)

    async def _invalidate_local(self, provider_id: str) -> None:
        async with self._lock:
            self._generation += 1
            inst = self._instances.pop(provider_id, None)
        if inst is not None:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001 - best-effort: the row is already written, a failed close must not fail the edit
                logger.warning(
                    "ArtifactStorageRegistry: aclose() on the dropped instance for %r failed: %s", provider_id, exc,
                )

    def _flush_local(self) -> None:
        """Drop every cached store WITHOUT closing it.

        Invoked (synchronously) when the invalidation subscription reconnects: a notification dropped during the blip may have left a stale store
        cached (a changed root, bucket or key), so every cached store is treated as stale and rebuilt by the next get. None is closed, because one may be
        in use by a request in flight; dropping the cache entry is enough to force a fresh build. No lock is taken (the hook runs synchronously in the
        bus's supervisor task and clearing a dict has no await point, so no coroutine sees a half-cleared state), which is why a ``get_provider``
        suspended at its row read can still land afterwards: bumping ``_generation`` makes it skip its cache insert.
        """
        self._generation += 1
        self._instances.clear()

    async def bind_invalidation_bus(self, bus: "InvalidationBus") -> None:
        """Wire the cache eviction to the bus, so an edit made in any process reaches this one. Idempotent."""
        if self._invalidation_bus is not None:
            return
        self._invalidation_bus = bus

        async def _on_event(provider_id: str) -> None:
            await self._invalidate_local(provider_id)

        try:
            subscription = await bus.subscribe(
                InvalidationTopic.ARTIFACT_STORAGE_PROVIDER, _on_event, on_reconnect=self._flush_local,
            )
        except BaseException:
            # Never leave the registry publishing to a bus it is not listening to: an unbound registry evicts locally.
            self._invalidation_bus = None
            raise
        self._invalidation_subs.append(subscription)

    async def aclose(self) -> None:
        for sub in self._invalidation_subs:
            try:
                await sub.aclose()
            except Exception:  # noqa: BLE001 - best-effort
                pass
        self._invalidation_subs.clear()
        # The subscription is gone, so a later ``invalidate`` has to evict here instead of publishing to no one.
        self._invalidation_bus = None
        async with self._lock:
            self._generation += 1
            instances = list(self._instances.values())
            self._instances.clear()
        for inst in instances:
            try:
                await inst.aclose()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "ArtifactStorageRegistry.aclose: instance close failed: %s", exc,
                )


def _default_factory(row, storage_provider):  # pragma: no cover - thin shim
    from primer.artifact.factory import build_artifact_storage

    return build_artifact_storage(row, storage_provider=storage_provider)


__all__ = ["ArtifactStorageRegistry", "DEFAULT_ARTIFACT_PROVIDER_ID"]
