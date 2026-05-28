"""Storage factory dispatch tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import (
    PoolConfig,
    PostgresConfig,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider


def test_factory_dispatches_to_sqlite(tmp_path: Path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "data.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    assert isinstance(provider, SqliteStorageProvider)


def test_factory_dispatches_to_postgres():
    cfg = StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname="h", username="u", password="p", database="d",
            pool=PoolConfig(),
        ),
    )
    provider = StorageProviderFactory.create(cfg)
    assert isinstance(provider, PostgresStorageProvider)


def test_factory_unknown_provider_raises():
    # Synthesise an unknown by bypassing the enum validator
    class _FakeCfg:
        provider = "totally-not-real"
        config = None
    with pytest.raises(ConfigError):
        StorageProviderFactory.create(_FakeCfg())  # type: ignore[arg-type]
