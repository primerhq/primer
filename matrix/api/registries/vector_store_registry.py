"""Single-active vector-store registry.

Reads its configuration from :class:`matrix.api.config.AppConfig` —
the vector store is infrastructure-level (mirroring the database) and
no longer lives in storage. ``None`` config means the subsystem is
disabled; ``get()`` raises :class:`ConfigError`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from matrix.model.except_ import ConfigError
from matrix.model.provider import VectorStoreProviderConfig


if TYPE_CHECKING:
    from matrix.int.vector_store import VectorStore
    from matrix.int.vector_store_provider import VectorStoreProvider


logger = logging.getLogger(__name__)


# Retained for backwards compatibility with code that still references
# the old in-storage row id; nothing in the API code path uses it now.
ACTIVE_VECTOR_STORE_CONFIG_ID = "_active_vector_store"


def _default_factory(
    config: VectorStoreProviderConfig,
) -> "VectorStoreProvider":  # pragma: no cover
    """Production dispatch via :class:`VectorStoreProviderFactory`.

    Local import keeps asyncpg / pgvector heavyweight imports off the
    hot startup path until a vector store is actually constructed.
    """
    from matrix.vector.factory import VectorStoreProviderFactory

    return VectorStoreProviderFactory.create(config)


class VectorStoreRegistry:
    """Cache + lifecycle for the (single) active vector store.

    Configuration is supplied at construction (typically from
    :attr:`AppConfig.vector_store`). ``None`` config = subsystem
    disabled; calls to :meth:`get` raise :class:`ConfigError`.
    """

    def __init__(
        self,
        config: VectorStoreProviderConfig | None,
        *,
        factory: Callable[[VectorStoreProviderConfig], "VectorStoreProvider"] | None = None,
    ) -> None:
        self._config = config
        self._factory = factory or _default_factory
        self._provider: "VectorStoreProvider | None" = None
        self._store: "VectorStore | None" = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """``True`` when a vector store config was supplied at construction."""
        return self._config is not None

    async def get(self) -> "VectorStore":
        if self._config is None:
            raise ConfigError(
                "no vector store configured; set ``vector_store`` in "
                "the AppConfig (env-prefix MATRIX_VECTOR_STORE__... or "
                "TOML [vector_store] section) and restart the service."
            )
        async with self._lock:
            if self._store is not None:
                return self._store
            provider = self._factory(self._config)
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
