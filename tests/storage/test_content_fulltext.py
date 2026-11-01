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


async def test_fulltext_path_prefix_wildcards_are_literal_like_the_listing(sp):
    """CV-4: ``list(prefix=)`` escapes the LIKE metacharacters; the search scope must too, or ``my_dir`` also reaches ``myXdir`` and ``100%`` reaches ``1000``."""
    store = sp.get_content_store()
    for i, path in enumerate(("my_dir/a", "myXdir/a", "100%/a", "1000/a")):
        await store.upsert(document_id=f"p{i}", collection_id="c", path=path, content="needle")
    assert sorted(h.path for h in await store.search_fulltext("c", "needle", path_prefix="my_dir")) == ["my_dir/a"]
    assert sorted(h.path for h in await store.search_fulltext("c", "needle", path_prefix="100%")) == ["100%/a"]
    assert sorted(h.path for h in await store.search_fulltext("c", "needle", path_prefix="my")) == ["myXdir/a", "my_dir/a"], "a plain prefix still matches"



async def test_fulltext_nul_in_the_query_never_raises(sp):
    """CV-5: the contract says user text cannot raise; a NUL byte used to reach the driver (a 500 from the search route)."""
    store = sp.get_content_store()
    await store.upsert(document_id="n1", collection_id="c", path="a", content="hello world foo bar")
    assert await store.search_fulltext("c", "\x00") == []
    assert await store.search_fulltext("c", "hel\x00lo")  == await store.search_fulltext("c", "hello"), "the NUL is dropped, the rest searches"
    assert [h.path for h in await store.search_fulltext("c", "wor\x00ld foo")] == ["a"]
