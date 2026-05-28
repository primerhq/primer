"""Factory that dispatches a :class:`StorageProviderConfig` to a concrete provider."""

from __future__ import annotations

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError
from primer.model.provider import StorageProviderConfig, StorageProviderType


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
            from primer.storage.postgres import PostgresStorageProvider

            return PostgresStorageProvider(config.config)  # type: ignore[arg-type]
        if config.provider == StorageProviderType.SQLITE:
            from primer.storage.sqlite import SqliteStorageProvider

            return SqliteStorageProvider(config.config)  # type: ignore[arg-type]
        raise ConfigError(f"unknown StorageProviderType {config.provider!r}")
