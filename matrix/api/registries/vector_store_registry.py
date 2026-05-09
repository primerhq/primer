"""Single-active vector-store registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from matrix.model.except_ import ConfigError
from matrix.model.vector import VectorStoreConfig


if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider
    from matrix.int.vector_store import VectorStore
    from matrix.int.vector_store_provider import VectorStoreProvider


logger = logging.getLogger(__name__)


ACTIVE_VECTOR_STORE_CONFIG_ID = "_active_vector_store"


def _default_factory(  # pragma: no cover
    config: VectorStoreConfig,
) -> "VectorStoreProvider":
    raise ConfigError(
        "default VectorStoreProvider factory not wired in Phase 0; "
        "supply a `factory` to VectorStoreRegistry"
    )


class VectorStoreRegistry:
    """Cache + lifecycle for the (single) active vector store."""

    def __init__(
        self,
        storage_provider: "StorageProvider",
        *,
        factory: Callable[[VectorStoreConfig], "VectorStoreProvider"] | None = None,
    ) -> None:
        self._sp = storage_provider
        self._factory = factory or _default_factory
        self._provider: "VectorStoreProvider | None" = None
        self._store: "VectorStore | None" = None
        self._lock = asyncio.Lock()

    async def get(self) -> "VectorStore":
        async with self._lock:
            if self._store is not None:
                return self._store
            row = await self._sp.get_storage(VectorStoreConfig).get(
                ACTIVE_VECTOR_STORE_CONFIG_ID
            )
            if row is None:
                raise ConfigError(
                    "no vector store configured; "
                    f"create a VectorStoreConfig row with id "
                    f"{ACTIVE_VECTOR_STORE_CONFIG_ID!r} via the Phase 3 API"
                )
            provider = self._factory(row)
            await provider.initialize()
            self._provider = provider
            self._store = provider.get_vector_store()
            return self._store

    async def get_provider(self) -> "VectorStoreProvider":
        await self.get()
        assert self._provider is not None
        return self._provider

    async def invalidate(self) -> None:
        async with self._lock:
            provider = self._provider
            self._provider = None
            self._store = None
        if provider is not None:
            await provider.aclose()

    async def aclose(self) -> None:
        await self.invalidate()


__all__ = ["ACTIVE_VECTOR_STORE_CONFIG_ID", "VectorStoreRegistry"]
