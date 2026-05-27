"""Dispatch a :class:`SchedulerProviderConfig` to the right impl.

Mirrors :class:`matrix.storage.factory.StorageProviderFactory` — local
imports inside the dispatch branches keep heavyweight modules
(asyncpg) off the import path until they're needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from matrix.int.scheduler import Scheduler
from matrix.model.except_ import ConfigError
from matrix.model.scheduler import (
    SchedulerProviderConfig,
    SchedulerProviderType,
)

if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


class SchedulerFactory:
    """Construct a :class:`Scheduler` from a discriminated config."""

    @staticmethod
    def create(
        config: SchedulerProviderConfig,
        *,
        storage_provider: "StorageProvider | None",
    ) -> Scheduler:
        """Return an un-initialised scheduler matching ``config.provider``.

        Caller is responsible for ``await scheduler.initialize()`` before
        using it and ``await scheduler.aclose()`` at shutdown.

        ``storage_provider`` is required for the Postgres impl (which
        reuses its connection pool); ignored by the in-memory impl.
        """
        if config.provider == SchedulerProviderType.IN_MEMORY:
            from matrix.scheduler.in_memory import InMemoryScheduler

            return InMemoryScheduler(storage_provider=storage_provider)
        if config.provider == SchedulerProviderType.POSTGRES:
            if storage_provider is None:
                raise ConfigError(
                    "Postgres scheduler requires a StorageProvider to "
                    "share its connection pool"
                )
            from matrix.scheduler.postgres import PostgresScheduler

            return PostgresScheduler(
                storage_provider=storage_provider,
                config=config.config,  # type: ignore[arg-type]
            )
        raise ConfigError(
            f"unknown SchedulerProviderType {config.provider!r}"
        )
