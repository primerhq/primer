"""Shared fixtures for storage-backend tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# tests/storage/test_postgres_provider.py has always used this fixture but
# it lives in tests/coordinator/conftest.py, which is out of scope for this
# directory: with PRIMER_TEST_POSTGRES_URL set those tests ERRORed at setup
# ("fixture not found"), and without it they skipped - so they never ran
# anywhere. Re-export it rather than keep a second copy.
from tests.coordinator.conftest import postgres_storage_provider  # noqa: F401


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
