"""Factory that dispatches a :class:`VectorStoreProviderConfig` to a concrete provider."""

from __future__ import annotations

from matrix.int.vector_store_provider import VectorStoreProvider
from matrix.model.except_ import ConfigError
from matrix.model.provider import (
    VectorStoreProviderConfig,
    VectorStoreProviderType,
)


class VectorStoreProviderFactory:
    """Construct a :class:`VectorStoreProvider` from a discriminated config.

    Two backends are supported today: pgvector and pgvectorscale. Both
    share the per-collection HNSW table layout; pgvectorscale also
    installs the ``vectorscale`` extension. Adding a new backend
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
            from matrix.vector.pgvector import PgVectorStoreProvider

            return PgVectorStoreProvider(config.config)  # type: ignore[arg-type]
        if config.provider == VectorStoreProviderType.PGVECTORSCALE:
            from matrix.vector.pgvectorscale import (
                PgVectorScaleStoreProvider,
            )

            return PgVectorScaleStoreProvider(config.config)  # type: ignore[arg-type]
        raise ConfigError(
            f"unknown VectorStoreProviderType {config.provider!r}"
        )
