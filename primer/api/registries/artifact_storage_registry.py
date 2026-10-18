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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.artifact.routing import RoutingArtifactStorage

# Reserved id of the auto-seeded default provider (the DB backend), so chat
# media works with zero operator configuration. Re-exported: it is defined in
# the leaf ``primer.model.providers.artifact`` (and re-exported by
# ``primer.artifact.factory``) because ``primer.artifact`` must not import
# ``primer.api`` (its check is imported by the system toolset, which
# ``primer.api`` imports).
from primer.artifact.factory import DEFAULT_ARTIFACT_PROVIDER_ID

if TYPE_CHECKING:
    from primer.int.artifact_storage import ArtifactStorage
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

    async def get_provider(self, provider_id: str) -> "ArtifactStorage":
        """Resolve a row to its live ArtifactStorage instance (cached per id)."""
        async with self._lock:
            cached = self._instances.get(provider_id)
        if cached is not None:
            return cached

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
                self._instances[provider_id] = provider
                close_loser = None
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
                default_provider_id=self._default_provider_id,
            )
        return self._routing

    async def invalidate(self, provider_id: str) -> None:
        async with self._lock:
            inst = self._instances.pop(provider_id, None)
        if inst is not None:
            await inst.aclose()

    async def aclose(self) -> None:
        async with self._lock:
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
