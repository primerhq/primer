"""Factory that dispatches a :class:`StorageProviderConfig` to a concrete provider."""

from __future__ import annotations

from matrix.int.storage_provider import StorageProvider
from matrix.model.except_ import ConfigError
from matrix.model.provider import StorageProviderConfig, StorageProviderType


class StorageProviderFactory:
    """Construct a :class:`StorageProvider` from a discriminated config.

    Currently only the Postgres backend is supported. Adding a new
    backend means adding a new :class:`StorageProviderType` enum
    value, a new ``*Config`` Pydantic model, and a new branch here --
    no other code needs to change.
    """

    @staticmethod
    def create(config: StorageProviderConfig) -> StorageProvider:
        """Return an un-initialised provider matching ``config.provider``.

        Caller is responsible for ``await provider.initialize()`` before
        using it and ``await provider.aclose()`` at shutdown.
        """
        if config.provider == StorageProviderType.POSTGRES:
            # Local import keeps the factory free of asyncpg /
            # heavyweight imports until a Postgres provider is asked for.
            from matrix.storage.postgres import PostgresStorageProvider

            return PostgresStorageProvider(config.config)
        raise ConfigError(f"unknown StorageProviderType {config.provider!r}")
