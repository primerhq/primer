"""Shared fixtures for storage-backend tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    """An initialised SqliteStorageProvider against a tmp file."""
    cfg = SqliteConfig(path=tmp_path / "data.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()
