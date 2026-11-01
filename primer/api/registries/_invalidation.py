"""Bus-wired cache eviction for a registry (GX-3), the way ``ProviderRegistry`` and ``ArtifactStorageRegistry`` already do it.

Every process has its own registry and so its own cache. A registry that bound the coordinator's invalidation bus publishes ``invalidate(key)`` on its
topic, and every process, the publisher included, evicts its own copy from its subscription; unbound (a test, a one-process run before the bus exists)
``invalidate`` evicts locally. A reconnect of the bus may have dropped evictions, so it flushes the whole cache (an extra rebuild is safe; a stale
adapter, a rotated key still in use, is not).

A subclass sets ``_invalidation_topic`` (None: no topic, binding does nothing) and provides ``_invalidate_local(key)`` and ``_flush_local()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.coordinator import InvalidationBus, InvalidationSubscription, InvalidationTopic


class BusInvalidated:
    _invalidation_topic: "InvalidationTopic | None" = None
    _invalidation_bus: "InvalidationBus | None" = None

    def _init_invalidation(self, topic: "InvalidationTopic | None") -> None:
        self._invalidation_topic = topic
        self._invalidation_bus = None
        self._invalidation_subs: "list[InvalidationSubscription]" = []

    async def _invalidate_local(self, key: str) -> None:  # pragma: no cover - supplied by the registry
        raise NotImplementedError

    def _flush_local(self) -> None:  # pragma: no cover - supplied by the registry
        raise NotImplementedError

    async def invalidate(self, key: str) -> None:
        """Evict ``key`` here and, with the bus bound, in every other process."""
        if self._invalidation_bus is not None and self._invalidation_topic is not None:
            await self._invalidation_bus.publish(self._invalidation_topic, key)
        else:
            await self._invalidate_local(key)

    async def bind_invalidation_bus(self, bus: "InvalidationBus") -> None:
        """Wire the eviction to the bus. Idempotent."""
        if self._invalidation_bus is not None or self._invalidation_topic is None:
            return
        self._invalidation_bus = bus
        try:
            subscription = await bus.subscribe(self._invalidation_topic, self._invalidate_local, on_reconnect=self._flush_local)
        except BaseException:
            self._invalidation_bus = None             # never publish to a bus this registry is not listening to
            raise
        self._invalidation_subs.append(subscription)

    async def _unbind_invalidation_bus(self) -> None:
        for sub in self._invalidation_subs:
            try:
                await sub.aclose()
            except Exception:  # noqa: BLE001 - best effort at shutdown
                pass
        self._invalidation_subs.clear()
        self._invalidation_bus = None                 # a later invalidate evicts here instead of publishing to no one
