"""Validation tests for StorageProviderConfig with the SQLite branch."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from matrix.model.provider import (
    PostgresConfig,
    PoolConfig,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)


def test_sqlite_enum_value_is_stable():
    assert StorageProviderType.SQLITE.value == "sqlite"


def test_sqlite_config_minimal_path_only(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "data.sqlite")
    assert cfg.path == tmp_path / "data.sqlite"
    assert cfg.busy_timeout_ms == 5000
    assert cfg.synchronous == "normal"
    assert cfg.journal_mode == "wal"


def test_sqlite_config_rejects_invalid_journal_mode(tmp_path: Path):
    with pytest.raises(ValidationError):
        SqliteConfig(path=tmp_path / "x.sqlite", journal_mode="off")  # type: ignore[arg-type]


def test_storage_provider_config_sqlite_branch(tmp_path: Path):
    sp = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "data.sqlite"),
    )
    assert sp.provider == StorageProviderType.SQLITE
    assert isinstance(sp.config, SqliteConfig)


def test_storage_provider_config_sqlite_with_postgres_config_rejected(tmp_path: Path):
    with pytest.raises(ValidationError) as ei:
        StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=PostgresConfig(
                hostname="h", username="u", password="p", database="d",
                pool=PoolConfig(),
            ),
        )
    assert "sqlite" in str(ei.value).lower()


def test_storage_provider_config_postgres_with_sqlite_config_rejected(tmp_path: Path):
    with pytest.raises(ValidationError) as ei:
        StorageProviderConfig(
            provider=StorageProviderType.POSTGRES,
            config=SqliteConfig(path=tmp_path / "x.sqlite"),
        )
    assert "postgres" in str(ei.value).lower()
