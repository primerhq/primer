"""Unit tests for :class:`primer.knowledge.document_service.DocumentService`.

Exercises the path-addressed create/read/list/delete/move surface over a
REAL sqlite provider in a tmp dir, including the transactional-write
guarantee that a failed content write leaves NO orphan entity row.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.document_service import DocumentService
from primer.model.collection import Document
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.storage import OffsetPage
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def svc(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    # P1 keeps search ON, but the service's indexer hook is optional; pass
    # None here so the unit tests do not need an embedder/vector store.
    yield DocumentService(provider, indexer=None)
    await provider.aclose()


async def test_create_then_read_by_path(svc):
    doc = await svc.upsert(
        collection_id="c1", path="concepts/slo.md", content="body", title="SLO"
    )
    assert doc.path == "concepts/slo.md" and doc.collection_id == "c1"
    res = await svc.read(collection_id="c1", path="concepts/slo.md")
    assert res.content == "body" and res.document.title == "SLO"
    assert res.document.id == doc.id


async def test_upsert_same_path_updates_same_doc(svc):
    d1 = await svc.upsert(collection_id="c1", path="a.md", content="v1")
    d2 = await svc.upsert(collection_id="c1", path="a.md", content="v2")
    assert d1.id == d2.id
    assert (await svc.read(collection_id="c1", path="a.md")).content == "v2"


async def test_read_missing_is_notfound(svc):
    with pytest.raises(NotFoundError):
        await svc.read(collection_id="c1", path="nope.md")


async def test_list_no_bodies(svc):
    await svc.upsert(collection_id="c1", path="docs/a.md", content="abc", title="A")
    await svc.upsert(collection_id="c1", path="docs/b.md", content="de")
    entries = await svc.list(collection_id="c1", prefix="docs/")
    paths = sorted(e.path for e in entries)
    assert paths == ["docs/a.md", "docs/b.md"]
    assert all(not hasattr(e, "content") for e in entries)


async def test_move(svc):
    await svc.upsert(collection_id="c1", path="a.md", content="x")
    await svc.move(collection_id="c1", src="a.md", dst="b.md")
    assert (await svc.read(collection_id="c1", path="b.md")).content == "x"
    with pytest.raises(NotFoundError):
        await svc.read(collection_id="c1", path="a.md")


async def test_move_mirrors_entity_path(svc):
    doc = await svc.upsert(collection_id="c1", path="a.md", content="x")
    await svc.move(collection_id="c1", src="a.md", dst="b.md")
    stored = await svc._docs.get(doc.id)
    assert stored is not None and stored.path == "b.md"


async def test_move_conflict(svc):
    await svc.upsert(collection_id="c1", path="a.md", content="x")
    await svc.upsert(collection_id="c1", path="b.md", content="y")
    with pytest.raises(ConflictError):
        await svc.move(collection_id="c1", src="a.md", dst="b.md")


async def test_move_missing_src_is_notfound(svc):
    with pytest.raises(NotFoundError):
        await svc.move(collection_id="c1", src="missing.md", dst="b.md")


async def test_delete(svc):
    await svc.upsert(collection_id="c1", path="a.md", content="x")
    await svc.delete(collection_id="c1", path="a.md")
    with pytest.raises(NotFoundError):
        await svc.read(collection_id="c1", path="a.md")
    # entity is gone too: the content row resolve returns None
    assert await svc._content.resolve_id("c1", "a.md") is None


async def test_delete_missing_is_notfound(svc):
    with pytest.raises(NotFoundError):
        await svc.delete(collection_id="c1", path="nope.md")


async def test_no_orphan_entity_when_content_write_fails(svc, monkeypatch):
    # Force the content upsert to fail AFTER the entity would be written;
    # assert NO entity row remains (the two writes share one transaction).
    async def boom(*a, **k):
        raise RuntimeError("content write failed")

    monkeypatch.setattr(svc._content, "upsert", boom)
    with pytest.raises(RuntimeError):
        await svc.upsert(collection_id="c1", path="orphan.md", content="x")
    # No Document entity should have been persisted for this path: scan the
    # entity store and assert none carries path == "orphan.md".
    page = await svc._docs.list(OffsetPage(offset=0, length=200))
    assert not [d for d in page.items if d.path == "orphan.md"]
    # And the content store has no row either (rollback covered both).
    assert await svc._content.resolve_id("c1", "orphan.md") is None


async def test_read_falls_back_to_entity_only_doc(svc):
    # Create a Document ENTITY directly (no content row), as the generic
    # CRUD route POST/PUT /v1/documents does: body lives in meta only.
    doc = Document(
        id="doc-entity-only",
        collection_id="c1",
        name="legacy",
        path="legacy/only.md",
        meta={"content": "X"},
    )
    await svc._docs.create(doc)
    # No content row exists for this path.
    assert await svc._content.resolve_id("c1", "legacy/only.md") is None

    # read() must fall back to the entity + its meta body.
    res = await svc.read(collection_id="c1", path="legacy/only.md")
    assert res.content == "X"
    assert res.document.id == doc.id

    # list() must surface the entity-only doc too.
    entries = await svc.list(collection_id="c1")
    paths = [e.path for e in entries]
    assert "legacy/only.md" in paths


async def test_read_truly_missing_still_404(svc):
    with pytest.raises(NotFoundError):
        await svc.read(collection_id="c1", path="does/not/exist.md")


async def test_list_dedups_content_row_over_entity(svc):
    # A doc with BOTH a content row and an entity row must appear ONCE,
    # with the content-row size winning.
    await svc.upsert(collection_id="c1", path="dup.md", content="hello")
    entries = await svc.list(collection_id="c1")
    dup = [e for e in entries if e.path == "dup.md"]
    assert len(dup) == 1
    assert dup[0].size == len("hello")


async def test_upsert_indexer_called_on_success(svc):
    calls: list[tuple[str, str]] = []

    async def indexer(*, document: Document, content: str) -> None:
        calls.append((document.path, content))

    svc._indexer = indexer
    doc = await svc.upsert(collection_id="c1", path="i.md", content="hello")
    assert calls == [(doc.path, "hello")]
