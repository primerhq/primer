"""Tests for primer.scheduler.SchedulerFactory."""

from __future__ import annotations

import pytest

from primer.int.scheduler import Scheduler
from primer.model.except_ import ConfigError
from primer.model.scheduler import (
    InMemorySchedulerConfig,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from primer.scheduler.factory import SchedulerFactory


def test_factory_creates_in_memory():
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.IN_MEMORY,
        config=InMemorySchedulerConfig(),
    )
    sched = SchedulerFactory.create(cfg, storage_provider=None)
    assert isinstance(sched, Scheduler)


def test_factory_postgres_requires_storage_provider():
    """Postgres impl needs the storage provider's connection pool."""
    from primer.model.scheduler import PostgresSchedulerConfig
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.POSTGRES,
        config=PostgresSchedulerConfig(),
    )
    with pytest.raises(ConfigError):
        SchedulerFactory.create(cfg, storage_provider=None)


def test_factory_postgres_rejects_non_postgres_storage(tmp_path):
    """Postgres scheduler + non-Postgres storage is a clean ConfigError.

    The Postgres scheduler reaches into ``storage_provider.pool`` (asyncpg),
    which SQLite storage does not expose. Guarding at the factory choke point
    turns a deep AttributeError inside ``initialize()`` into a clear message.
    """
    from primer.model.scheduler import PostgresSchedulerConfig
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sqlite_provider = SqliteStorageProvider(
        SqliteConfig(path=tmp_path / "s.sqlite")
    )
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.POSTGRES,
        config=PostgresSchedulerConfig(),
    )
    with pytest.raises(ConfigError) as exc:
        SchedulerFactory.create(cfg, storage_provider=sqlite_provider)
    # The message must name the offending storage provider type.
    assert "SqliteStorageProvider" in str(exc.value)


def test_factory_rejects_unknown():
    """Defensive: future provider type with no impl branch should raise."""

    class _Fake:
        provider = "mystery"
        config = None

    with pytest.raises(ConfigError):
        SchedulerFactory.create(_Fake(), storage_provider=None)  # type: ignore[arg-type]
