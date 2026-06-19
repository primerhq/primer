"""Parametrised :class:`DocumentContentStore` contract - every backend.

Each scenario is asserted on SQLite always, and on Postgres when
``PRIMER_TEST_PG_DSN`` is set (a later task adds the Postgres impl so
its arm passes). The point is to catch a semantic divergence the moment
it appears, not to re-test the per-backend translator.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.document_content import DocumentContentStore
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


_BACKENDS: list[str] = ["sqlite"]
if os.environ.get("PRIMER_TEST_PG_DSN"):
    _BACKENDS.append("postgres")


@pytest_asyncio.fixture(params=_BACKENDS)
async def content_store(
    request: pytest.FixtureRequest, tmp_path: Path,
) -> AsyncIterator[DocumentContentStore]:
    backend = request.param
    if backend == "sqlite":
        cfg = StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "content.sqlite"),
        )
    else:
        pytest.skip("postgres contract path requires PRIMER_TEST_PG_DSN")
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    try:
        store = provider.get_content_store()
        await store.ensure_schema()
        yield store
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_upsert_get_roundtrip(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="hello")
    assert await content_store.get("d1") == "hello"
    row = await content_store.get_by_path("c", "a.md")
    assert row is not None and row.document_id == "d1" and row.content == "hello"


@pytest.mark.asyncio
async def test_resolve_id_without_body(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="x")
    assert await content_store.resolve_id("c", "a.md") == "d1"
    assert await content_store.resolve_id("c", "missing.md") is None


@pytest.mark.asyncio
async def test_upsert_same_doc_updates(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="v1")
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="v2")
    assert await content_store.get("d1") == "v2"


@pytest.mark.asyncio
async def test_path_uniqueness_conflict(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="x")
    with pytest.raises(ConflictError):
        await content_store.upsert(document_id="d2", collection_id="c", path="a.md", content="y")


@pytest.mark.asyncio
async def test_same_path_different_collections_ok(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c1", path="a.md", content="x")
    await content_store.upsert(document_id="d2", collection_id="c2", path="a.md", content="y")
    assert await content_store.get("d1") == "x" and await content_store.get("d2") == "y"


@pytest.mark.asyncio
async def test_list_never_returns_body(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="docs/a.md", content="abc")
    await content_store.upsert(document_id="d2", collection_id="c", path="docs/b.md", content="de")
    await content_store.upsert(document_id="d3", collection_id="c", path="other/c.md", content="f")
    entries = await content_store.list("c", prefix="docs/")
    paths = sorted(e.path for e in entries)
    assert paths == ["docs/a.md", "docs/b.md"]
    assert all(not hasattr(e, "content") for e in entries)
    by_path = {e.path: e for e in entries}
    assert by_path["docs/a.md"].size == 3


@pytest.mark.asyncio
async def test_move_changes_path(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="x")
    await content_store.move("d1", "b.md")
    assert await content_store.get_by_path("c", "b.md") is not None
    assert await content_store.get_by_path("c", "a.md") is None


@pytest.mark.asyncio
async def test_move_conflict(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="x")
    await content_store.upsert(document_id="d2", collection_id="c", path="b.md", content="y")
    with pytest.raises(ConflictError):
        await content_store.move("d1", "b.md")


@pytest.mark.asyncio
async def test_move_missing(content_store: DocumentContentStore) -> None:
    with pytest.raises(NotFoundError):
        await content_store.move("nope", "b.md")


@pytest.mark.asyncio
async def test_delete_then_absent(content_store: DocumentContentStore) -> None:
    await content_store.upsert(document_id="d1", collection_id="c", path="a.md", content="x")
    await content_store.delete("d1")
    assert await content_store.get("d1") is None


@pytest.mark.asyncio
async def test_delete_absent_is_noop(content_store: DocumentContentStore) -> None:
    await content_store.delete("nope")  # must not raise
