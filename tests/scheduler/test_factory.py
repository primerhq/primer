"""Tests for matrix.scheduler.SchedulerFactory."""

from __future__ import annotations

import pytest

from matrix.int.scheduler import Scheduler
from matrix.model.except_ import ConfigError
from matrix.model.scheduler import (
    InMemorySchedulerConfig,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from matrix.scheduler.factory import SchedulerFactory


def test_factory_creates_in_memory():
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.IN_MEMORY,
        config=InMemorySchedulerConfig(),
    )
    sched = SchedulerFactory.create(cfg, storage_provider=None)
    assert isinstance(sched, Scheduler)


def test_factory_postgres_requires_storage_provider():
    """Postgres impl needs the storage provider's connection pool."""
    from matrix.model.scheduler import PostgresSchedulerConfig
    cfg = SchedulerProviderConfig(
        provider=SchedulerProviderType.POSTGRES,
        config=PostgresSchedulerConfig(),
    )
    with pytest.raises(ConfigError):
        SchedulerFactory.create(cfg, storage_provider=None)


def test_factory_rejects_unknown():
    """Defensive: future provider type with no impl branch should raise."""

    class _Fake:
        provider = "mystery"
        config = None

    with pytest.raises(ConfigError):
        SchedulerFactory.create(_Fake(), storage_provider=None)  # type: ignore[arg-type]
