"""Factory that dispatches a :class:`VectorStoreProviderConfig` to a concrete provider."""

from __future__ import annotations

from primer.int.vector_store_provider import VectorStoreProvider
from primer.model.except_ import ConfigError
from primer.model.provider import (
    VectorStoreProviderConfig,
    VectorStoreProviderType,
)


class VectorStoreProviderFactory:
    """Construct a :class:`VectorStoreProvider` from a discriminated config.

    Three backends are supported: pgvector, pgvectorscale, and lance
    (LanceDB embedded). The first two share the per-collection HNSW
    table layout on Postgres; lance persists every collection as a
    Lance dataset under a single directory. Adding a new backend
    means adding a new :class:`VectorStoreProviderType` enum value,
    a new ``*Config`` Pydantic model, and a new branch here.
    """

    @staticmethod
    def create(config: VectorStoreProviderConfig) -> VectorStoreProvider:
        """Return an un-initialised provider matching ``config.provider``.

        Caller is responsible for ``await provider.initialize()`` before
        using it and ``await provider.aclose()`` at shutdown.
        """
        if config.provider == VectorStoreProviderType.PGVECTOR:
            from primer.vector.pgvector import PgVectorStoreProvider

            return PgVectorStoreProvider(config.config)  # type: ignore[arg-type]
        if config.provider == VectorStoreProviderType.PGVECTORSCALE:
            from primer.vector.pgvectorscale import (
                PgVectorScaleStoreProvider,
            )

            return PgVectorScaleStoreProvider(config.config)  # type: ignore[arg-type]
        if config.provider == VectorStoreProviderType.LANCE:
            from primer.vector.lance import LanceVectorStoreProvider

            return LanceVectorStoreProvider(config.config)  # type: ignore[arg-type]
        raise ConfigError(
            f"unknown VectorStoreProviderType {config.provider!r}"
        )
