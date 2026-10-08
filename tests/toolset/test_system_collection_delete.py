"""The system ``delete_collection`` tool deletes what the collection owns, as ``DELETE /v1/collections/{id}`` does (ticket 01a1131f "F").

The tool half of ``tests/api/test_collection_delete_cascades.py``; the semantics are the same and are explained there. The tool used to
remove only the collection row (RUN on real sqlite, D3 probe scenario F): ``list_documents`` still listed every document, the content rows
stayed, and the vector chunks stayed searchable. The cascade is one shared function (``primer.knowledge.lifecycle``), so the two surfaces
cannot drift again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.model.collection import Collection, Document
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteDocumentContentStore

# Re-export so pytest can resolve the real-sqlite system toolset with a search-enabled collection ("kb-1") and a recording vector store.
from tests.toolset.test_system_document_tools import _call, _put, world  # noqa: F401


def _watch_drops(store) -> list[str]:
    """Make the world's recording store drop a namespace's records, and say which namespaces were dropped."""
    dropped: list[str] = []

    async def drop_collection(collection_id):
        dropped.append(collection_id)
        for key in [k for k in store.records if k[0] == collection_id]:
            del store.records[key]

    store.drop_collection = drop_collection
    return dropped


async def _documents_of(sp, collection_id: str) -> list[Document]:
    page = await sp.get_storage(Document).find(
        Q(Document).where("collection_id", collection_id).build(), OffsetPage(offset=0, length=200),
    )
    return list(page.items)


@pytest.mark.asyncio
async def test_deleting_a_collection_through_the_tool_removes_documents_content_and_chunks(world) -> None:
    sp, toolset, store = world
    dropped = _watch_drops(store)
    await _put(toolset, "a.md")
    await _put(toolset, "notes/b.md")
    assert store.doc_ids(), "precondition: the documents were indexed"

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert not is_error, body
    assert await sp.get_storage(Collection).get("kb-1") is None
    assert await _documents_of(sp, "kb-1") == [], "the documents outlived their collection"
    content = sp.get_content_store()
    assert [p for p in ("a.md", "notes/b.md") if await content.resolve_id("kb-1", p) is not None] == [], "content rows outlived it"
    assert dropped == ["kb-1"], "the collection's vector namespace was not dropped"
    assert store.doc_ids() == set(), "a deleted collection's chunks are still searchable"


@pytest.mark.asyncio
async def test_a_collection_recreated_under_the_same_id_starts_empty(world) -> None:
    sp, toolset, store = world
    _watch_drops(store)
    await _put(toolset, "old.md")
    assert not (await _call(toolset, "delete_collection", id="kb-1"))[0]

    is_error, body = await _call(toolset, "create_collection", entity={"id": "kb-1", "description": "again"})

    assert not is_error, body
    assert await _documents_of(sp, "kb-1") == []
    is_error, listed = await _call(toolset, "list_documents", collection_id="kb-1")
    assert not is_error and listed["documents"] == [], "the old documents resurfaced under the reused id"


@pytest.mark.asyncio
async def test_a_vector_store_that_cannot_be_reached_refuses_the_delete_and_changes_nothing(world) -> None:
    sp, toolset, store = world
    await _put(toolset, "a.md")

    async def down(collection_id):
        raise ConnectionError("the vector store is down")

    store.drop_collection = down

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert is_error, "the delete reported success although the vector namespace could not be dropped"
    assert body["type"] == "provider-error", body
    assert "kb-1" in body["message"] and "search" in body["message"], "the refusal must say how to get unstuck"
    assert await sp.get_storage(Collection).get("kb-1") is not None
    assert len(await _documents_of(sp, "kb-1")) == 1
    assert await sp.get_content_store().resolve_id("kb-1", "a.md") is not None


@pytest.mark.asyncio
async def test_a_harness_managed_collection_is_refused_and_nothing_is_cascaded(world) -> None:
    sp, toolset, store = world
    await sp.get_storage(Collection).create(Collection(id="owned", description="owned by a harness", harness_id="hns_x"))
    doc = Document(id="doc-owned", collection_id="owned", slug="a", path="a.md", title="A")
    await sp.get_storage(Document).create(doc)
    await sp.get_content_store().upsert(document_id=doc.id, collection_id="owned", path="a.md", content="keep me")

    is_error, body = await _call(toolset, "delete_collection", id="owned")

    assert is_error, body
    assert await sp.get_storage(Collection).get("owned") is not None
    assert [d.path for d in await _documents_of(sp, "owned")] == ["a.md"]
    assert await sp.get_content_store().resolve_id("owned", "a.md") is not None


@pytest.mark.asyncio
async def test_deleting_a_collection_that_does_not_exist_is_still_not_found(world) -> None:
    _, toolset, _ = world

    is_error, body = await _call(toolset, "delete_collection", id="nope")

    assert is_error and body["type"] == "not-found", body


@pytest.mark.asyncio
async def test_the_delete_is_not_interruptible_because_it_is_several_steps(world) -> None:
    """A Stop between the vector drop and the row delete would leave a half-deleted collection."""
    _, toolset, _ = world

    declared = {tool.id: tool.interruptible async for tool in toolset.list_tools()}

    assert declared["delete_collection"] is False


@pytest.mark.asyncio
async def test_a_system_collection_cannot_be_deleted_and_nothing_is_cascaded(world) -> None:
    """``world`` seeds ``sys-1`` as a system collection. The tool deleted it (RUN: deleted=True) although docs/agents/knowledge.md says the
    CRUD layer refuses; with the cascade it would also have emptied it."""
    sp, toolset, _ = world
    doc = Document(id="doc-sys", collection_id="sys-1", slug="map", path="map.md", title="Map")
    await sp.get_storage(Document).create(doc)
    await sp.get_content_store().upsert(document_id=doc.id, collection_id="sys-1", path="map.md", content="regenerated")

    is_error, body = await _call(toolset, "delete_collection", id="sys-1")

    assert is_error and body["type"] == "forbidden", body
    assert "system-owned and read-only" in body["message"]
    assert await sp.get_storage(Collection).get("sys-1") is not None
    assert [d.path for d in await _documents_of(sp, "sys-1")] == ["map.md"]
    assert await sp.get_content_store().resolve_id("sys-1", "map.md") is not None


@pytest.mark.asyncio
async def test_the_cascade_runs_before_the_collection_row_is_deleted(world, monkeypatch) -> None:
    """A failure part-way must leave a collection that deleting again finishes, never documents without a collection: the row goes last."""
    sp, toolset, store = world
    await _put(toolset, "a.md")
    await _put(toolset, "b.md")
    log: list[str] = []

    def around(label, real):
        async def wrapper(*args, **kwargs):
            log.append(label)
            return await real(*args, **kwargs)
        return wrapper

    _watch_drops(store)  # the world's store drops nothing by itself
    monkeypatch.setattr(store, "drop_collection", around("vectors", store.drop_collection))
    docs, colls = sp.get_storage(Document), sp.get_storage(Collection)
    monkeypatch.setattr(docs, "delete", around("document", docs.delete))
    monkeypatch.setattr(colls, "delete", around("row", colls.delete))
    real_content = SqliteDocumentContentStore.delete

    async def content_delete(self, *args, **kwargs):
        log.append("content")
        return await real_content(self, *args, **kwargs)

    monkeypatch.setattr(SqliteDocumentContentStore, "delete", content_delete)

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert not is_error, body
    assert log.count("row") == 1 and log[-1] == "row", f"the row was not the last thing removed: {log}"
    assert log[0] == "vectors", f"the vector namespace was not dropped first: {log}"
    assert log.count("document") == 2 and log.count("content") == 2


@pytest.mark.asyncio
async def test_chunks_written_by_an_indexing_pass_that_lands_mid_purge_are_dropped_too(world, monkeypatch) -> None:
    sp, toolset, store = world
    dropped = _watch_drops(store)
    await _put(toolset, "a.md")
    docs = sp.get_storage(Document)
    real, landed = docs.delete, []

    async def indexing_lands(id, *, conn=None):
        if not landed:
            landed.append(id)
            store.records[("kb-1", id, "late")] = SimpleNamespace(collection_id="kb-1", document_id=id, chunk_id="late")
        return await real(id, conn=conn)

    monkeypatch.setattr(docs, "delete", indexing_lands)

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert not is_error, body
    assert landed, "the scenario never ran"
    assert store.doc_ids() == set(), "a chunk written mid-purge survived the delete"
    assert len(dropped) >= 2, "the namespace was dropped only before the documents, not after"


@pytest.mark.asyncio
async def test_a_collection_that_keeps_receiving_documents_is_not_deleted_and_says_so(world) -> None:
    sp, toolset, store = world
    dropped = _watch_drops(store)
    await _put(toolset, "a.md")
    drop = store.drop_collection

    async def writer_that_never_stops(collection_id):
        await drop(collection_id)
        if len(dropped) >= 2:
            await _put(toolset, f"late-{len(dropped)}.md")

    store.drop_collection = writer_that_never_stops

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert is_error and body["type"] == "conflict", body
    assert "kb-1" in body["message"]
    assert len(dropped) <= 6, "the purge chased the writer without a bound"
    assert await sp.get_storage(Collection).get("kb-1") is not None


@pytest.mark.asyncio
async def test_a_delete_that_removes_nothing_is_a_storage_error_not_a_vector_store_fault(world, monkeypatch) -> None:
    sp, toolset, store = world
    _watch_drops(store)
    await _put(toolset, "a.md")

    async def removes_nothing(id, *, conn=None):
        return None

    monkeypatch.setattr(sp.get_storage(Document), "delete", removes_nothing)

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert is_error and body["type"] == "storage-error", body
    assert await sp.get_storage(Collection).get("kb-1") is not None


@pytest.mark.asyncio
async def test_a_document_deleted_concurrently_counts_as_already_deleted(world, monkeypatch) -> None:
    sp, toolset, store = world
    _watch_drops(store)
    await _put(toolset, "a.md")
    await _put(toolset, "b.md")
    docs = sp.get_storage(Document)
    real, raced = docs.delete, []

    async def deleted_by_someone_else_first(id, *, conn=None):
        if not raced:
            raced.append(id)
            await real(id)
        return await real(id, conn=conn)

    monkeypatch.setattr(docs, "delete", deleted_by_someone_else_first)

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert not is_error, body
    assert raced, "the scenario never ran"
    assert await _documents_of(sp, "kb-1") == []


@pytest.mark.asyncio
async def test_content_rows_with_no_document_entity_are_removed_with_the_collection(world) -> None:
    sp, toolset, store = world
    _watch_drops(store)
    content = sp.get_content_store()
    await content.upsert(document_id="ghost-1", collection_id="kb-1", path="ghost.md", content="no entity row")
    await content.upsert(document_id="ghost-2", collection_id="elsewhere", path="ghost.md", content="another collection's")

    is_error, body = await _call(toolset, "delete_collection", id="kb-1")

    assert not is_error, body
    assert await content.resolve_id("kb-1", "ghost.md") is None, "an entity-less content row outlived its collection"
    assert await content.resolve_id("elsewhere", "ghost.md") == "ghost-2", "another collection's content row was removed"
