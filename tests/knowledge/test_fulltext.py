"""The fulltext knowledge shim: grep.py's shape over the FTS rung."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.knowledge.fulltext import fulltext_collection
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_fulltext_collection_caps_and_flags_truncation(sp):
    store = sp.get_content_store()
    for i in range(5):
        await store.upsert(document_id=f"d{i}", collection_id="kb",
                           path=f"note{i}", content="pelican pelican")
    res = await fulltext_collection(
        store, collection_id="kb", query="pelican", max_results=3,
    )
    assert len(res.hits) == 3 and res.truncated is True
    assert {h.path for h in res.hits} <= {f"note{i}" for i in range(5)}


async def test_fulltext_collection_uncapped_is_not_truncated(sp):
    store = sp.get_content_store()
    await store.upsert(document_id="d1", collection_id="kb",
                       path="only", content="a single pelican")
    res = await fulltext_collection(store, collection_id="kb", query="pelican")
    assert [h.path for h in res.hits] == ["only"]
    assert res.truncated is False
