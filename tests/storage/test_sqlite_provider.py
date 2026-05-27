"""Lifecycle tests for SqliteStorageProvider."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from matrix.model.except_ import ConfigError
from matrix.model.provider import SqliteConfig
from matrix.storage.sqlite import SqliteStorage, SqliteStorageProvider


@pytest.mark.asyncio
async def test_initialize_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "deep" / "deeper" / "data.sqlite"
    assert not nested.parent.exists()
    provider = SqliteStorageProvider(SqliteConfig(path=nested))
    try:
        await provider.initialize()
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_initialize_applies_pragmas(tmp_path: Path):
    cfg = SqliteConfig(
        path=tmp_path / "data.sqlite",
        journal_mode="wal",
        synchronous="normal",
        busy_timeout_ms=7000,
    )
    provider = SqliteStorageProvider(cfg)
    try:
        await provider.initialize()
        # journal_mode=WAL is persisted in the file header — a fresh
        # probe connection will see it.
        async with aiosqlite.connect(str(cfg.path)) as probe:
            cur = await probe.execute("PRAGMA journal_mode")
            row = await cur.fetchone()
            assert row is not None and row[0].lower() == "wal"
        # synchronous is a per-connection pragma (not stored in the file);
        # verify it via the provider's own shared connection instead.
        cur = await provider.connection.execute("PRAGMA synchronous")
        row = await cur.fetchone()
        # 1 = NORMAL per https://sqlite.org/pragma.html#pragma_synchronous
        assert row is not None and int(row[0]) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_initialize_idempotent(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "x.sqlite"))
    try:
        await provider.initialize()
        await provider.initialize()  # must not raise
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_aclose_idempotent(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "x.sqlite"))
    await provider.aclose()  # never initialised — no-op
    await provider.initialize()
    await provider.aclose()
    await provider.aclose()  # second close — no-op


@pytest.mark.asyncio
async def test_get_storage_returns_same_instance_for_same_model(
    sqlite_provider: SqliteStorageProvider,
):
    from matrix.model.common import Identifiable

    class _A(Identifiable):
        v: int

    h1 = sqlite_provider.get_storage(_A)
    h2 = sqlite_provider.get_storage(_A)
    assert h1 is h2
    assert isinstance(h1, SqliteStorage)


@pytest.mark.asyncio
async def test_get_storage_before_initialize_raises(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "x.sqlite"))
    from matrix.model.common import Identifiable

    class _A(Identifiable):
        v: int

    handle = provider.get_storage(_A)
    # handle is returned but using it before initialize must fail loudly.
    with pytest.raises(ConfigError):
        await handle.get("anything")


@pytest.mark.asyncio
async def test_sqlite_provider_creates_leases_table(tmp_path: Path):
    """initialize() must create the leases table for use by the claim engine."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        cur = await provider.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='leases'"
        )
        row = await cur.fetchone()
        assert row is not None, "leases table should exist after initialize()"
    finally:
        await provider.aclose()
