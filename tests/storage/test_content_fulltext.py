"""Full-text search on the document content store (SQLite FTS5).

The Postgres implementation shares the interface and is exercised by the
live-server e2e lane; these tests pin the contract on the backend the
unit lanes run.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed(store) -> None:
    await store.upsert(document_id="d1", collection_id="kb",
                       path="guides/refunds",
                       content="Refunds are processed within five days.")
    await store.upsert(document_id="d2", collection_id="kb",
                       path="guides/shipping",
                       content="Shipping takes two days by courier.")
    await store.upsert(document_id="d3", collection_id="other",
                       path="misc", content="Refunds elsewhere.")


async def test_fulltext_finds_by_keyword_scoped_to_collection(sp):
    store = sp.get_content_store()
    await _seed(store)
    hits = await store.search_fulltext("kb", "refunds")
    assert [h.path for h in hits] == ["guides/refunds"]
    assert "Refunds" in hits[0].excerpt
    assert isinstance(hits[0].score, float)


async def test_fulltext_respects_path_prefix_and_limit(sp):
    store = sp.get_content_store()
    await _seed(store)
    # Both kb documents mention "days"; the prefix narrows to one.
    hits = await store.search_fulltext("kb", "days", path_prefix="guides/s")
    assert [h.path for h in hits] == ["guides/shipping"]
    hits = await store.search_fulltext("kb", "days", limit=1)
    assert len(hits) == 1


async def test_fulltext_tracks_updates_and_deletes(sp):
    """The FTS index follows the row, not the first write."""
    store = sp.get_content_store()
    await _seed(store)
    await store.upsert(document_id="d1", collection_id="kb",
                       path="guides/refunds",
                       content="Returns policy changed entirely.")
    assert await store.search_fulltext("kb", "refunds") == []
    assert [h.path for h in await store.search_fulltext("kb", "returns")] \
        == ["guides/refunds"]
    await store.delete("d1")
    assert await store.search_fulltext("kb", "returns") == []


async def test_fulltext_query_punctuation_is_not_syntax(sp):
    """User text must never reach FTS5 as operators."""
    store = sp.get_content_store()
    await _seed(store)
    # Every one of these is an FTS5 syntax error if passed raw to MATCH.
    for query in ('what is a "refund" (fast)?', "AND", "refund*", "-days"):
        hits = await store.search_fulltext("kb", query)
        assert isinstance(hits, list)


async def test_fulltext_empty_query_returns_nothing(sp):
    store = sp.get_content_store()
    await _seed(store)
    assert await store.search_fulltext("kb", "   ") == []


async def test_fulltext_indexes_rows_that_predate_the_index(sp):
    """Upgrade path: rows written before FTS existed are searchable.

    ensure_schema re-runs on every boot; its rebuild step must fold in
    rows that were written when no FTS table existed.
    """
    store = sp.get_content_store()
    await _seed(store)
    await store.ensure_schema()
    assert [h.path for h in await store.search_fulltext("kb", "courier")] \
        == ["guides/shipping"]
