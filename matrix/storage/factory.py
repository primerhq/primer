"""Factory that dispatches a :class:`StorageProviderConfig` to a concrete provider."""

from __future__ import annotations

from matrix.int.storage_provider import StorageProvider
from matrix.model.except_ import ConfigError
from matrix.model.provider import StorageProviderConfig, StorageProviderType


class StorageProviderFactory:
    """Construct a :class:`StorageProvider` from a discriminated config.

    Two backends are supported:

    * ``POSTGRES`` -- production-grade JSONB+GIN tables in Postgres.
    * ``SQLITE`` -- embedded single-file backend for zero-config local
      use. Pairs only with the ``in_memory`` scheduler (single-process).
    """

    @staticmethod
    def create(config: StorageProviderConfig) -> StorageProvider:
        """Return an un-initialised provider matching ``config.provider``.

        Caller is responsible for ``await provider.initialize()`` before
        using it and ``await provider.aclose()`` at shutdown.
        """
        if config.provider == StorageProviderType.POSTGRES:
            from matrix.storage.postgres import PostgresStorageProvider

            return PostgresStorageProvider(config.config)  # type: ignore[arg-type]
        if config.provider == StorageProviderType.SQLITE:
            from matrix.storage.sqlite import SqliteStorageProvider

            return SqliteStorageProvider(config.config)  # type: ignore[arg-type]
        raise ConfigError(f"unknown StorageProviderType {config.provider!r}")
