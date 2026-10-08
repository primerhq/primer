"""Deleting a collection deletes what it owns: its documents, their content rows and its vector chunks (ticket 01a1131f "F").

RUN through the system tool on real sqlite (D3 probe, scenario F): after ``delete_collection`` of a collection holding documents the
collection row was gone but every document, every content row (``UNIQUE(collection_id, path)``) and every vector chunk was still there,
and the chunks were still searchable. ``collection_router`` is a bare ``make_crud_router``; nothing cascaded. Worse, a collection created
later under the same id (ids are chosen by the caller) found the old documents and chunks waiting for it.

The cascade runs BEFORE the collection row is removed and in this order: the vector namespace (one ``drop_collection``, so the cost does not
grow with the collection), then the documents with their content rows in batches of one transaction each, the namespace again and a last look
at the documents (a write in flight during the delete recreates the namespace or adds a document; bounded rounds, then a 409), a sweep of the
content rows by collection id, then the row. It is synchronous:
collections are text-only and bounded (a 1 MiB cap per document), the vector side is one call, and the caller learns the outcome from the
response, as ``PUT .../search`` (the backfill) already does. A vector store that cannot be reached REFUSES the delete (502) and changes
nothing, because swallowing it would leave chunks that resurface under a reused id; a provider or namespace that is simply gone does not block
it. To delete a collection whose vector store is permanently broken, ``DELETE .../search`` first (it always works), then delete.

The tool side is pinned in ``tests/toolset/test_system_collection_delete.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from primer.knowledge.document_service import DocumentService
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.except_ import NotFoundError, PrimerError
from primer.model.storage import OffsetPage
from primer.storage.q import Q
from primer.storage.sqlite import SqliteDocumentContentStore

# Re-export so pytest can resolve the sqlite-backed app, client and search-enabled collection fixtures.
from tests.api.test_knowledge_documents_by_path import app, client, collection_id, provider  # noqa: F401
from tests.knowledge.test_indexing import _Emb


class _Vectors:
    """A vector store that remembers its namespaces, as the real ones do, and can be made to fail."""

    def __init__(self) -> None:
        self.records: list = []
        self.namespaces: set[str] = set()
        self.dropped: list[str] = []
        self.drop_error: Exception | None = None
        self.drop_calls = 0
        self.on_drop = None  # async callable(n): what happens while the n-th drop of a namespace is in flight

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.namespaces.add(cid)

    async def put(self, record):
        self.records.append(record)

    async def get(self, cid, document_id):
        return [r for r in self.records if r.collection_id == cid and r.document_id == document_id]

    async def delete(self, cid, document_id):
        self.records = [r for r in self.records if not (r.collection_id == cid and r.document_id == document_id)]

    async def drop_collection(self, cid):
        self.drop_calls += 1
        if self.on_drop is not None:
            await self.on_drop(self.drop_calls)
        if self.drop_error is not None:
            raise self.drop_error
        self.dropped.append(cid)
        self.namespaces.discard(cid)
        self.records = [r for r in self.records if r.collection_id != cid]

    async def search_by_meta(self, cid, meta=None):
        return [r for r in self.records if r.collection_id == cid]

    def chunks_of(self, cid: str) -> list:
        return [r for r in self.records if r.collection_id == cid]


@pytest.fixture
def vectors(app) -> _Vectors:
    store = _Vectors()
    app.state.provider_registry.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    app.state.semantic_search_registry.get_store = AsyncMock(return_value=store)
    return store


def _docs_url(cid: str) -> str:
    return f"/v1/collections/{cid}/documents"


async def _put(client, cid: str, *paths: str) -> None:
    for path in paths:
        r = await client.put(_docs_url(cid), params={"path": path}, json={"content": f"the body of {path}"})
        assert r.status_code in (200, 201), r.text


async def _documents_of(provider, cid: str) -> list[Document]:
    out: list[Document] = []
    offset = 0
    while True:
        page = await provider.get_storage(Document).find(
            Q(Document).where("collection_id", cid).build(), OffsetPage(offset=offset, length=200),
        )
        out.extend(page.items)
        if len(page.items) < 200:
            return out
        offset += 200


async def _content_rows(provider, cid: str, paths: list[str]) -> list[str]:
    store = provider.get_content_store()
    return [p for p in paths if await store.resolve_id(cid, p) is not None]


@pytest.mark.asyncio
async def test_deleting_a_collection_removes_its_documents_and_content_rows(client, provider, collection_id, vectors):
    paths = ["a.md", "notes/b.md", "notes/deep/c.md"]
    await _put(client, collection_id, *paths)
    assert len(await _documents_of(provider, collection_id)) == 3

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert await provider.get_storage(Collection).get(collection_id) is None
    assert await _documents_of(provider, collection_id) == [], "the documents outlived their collection"
    assert await _content_rows(provider, collection_id, paths) == [], "the content rows outlived their collection"


@pytest.mark.asyncio
async def test_deleting_a_collection_removes_its_vector_chunks(client, collection_id, vectors):
    await _put(client, collection_id, "a.md", "b.md")
    assert vectors.chunks_of(collection_id), "precondition: the documents were indexed"

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    # Twice on purpose: before the documents, and again after them for the chunks of an indexing pass that was in flight (see below).
    assert vectors.dropped == [collection_id, collection_id], "the collection's vector namespace was not dropped before and after"
    assert vectors.chunks_of(collection_id) == [], "a deleted collection's chunks are still searchable"


@pytest.mark.asyncio
async def test_a_collection_created_again_under_the_same_id_starts_empty(client, provider, collection_id, vectors):
    await _put(client, collection_id, "old.md")
    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 204

    again = await client.post("/v1/collections", json=Collection(id=collection_id, description="new").model_dump(mode="json"))

    assert again.status_code == 201, again.text
    assert await _documents_of(provider, collection_id) == []
    listed = await client.get(_docs_url(collection_id))
    assert listed.status_code == 200 and listed.json()["documents"] == [], "the old documents resurfaced under the reused id"
    assert vectors.chunks_of(collection_id) == []


@pytest.mark.asyncio
async def test_another_collections_documents_and_chunks_are_untouched(client, provider, collection_id, vectors):
    other = Collection(id="kb-2", description="other", search=CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"), vector_store_provider_id="ssp-test",
    ))
    assert (await client.post("/v1/collections", json=other.model_dump(mode="json"))).status_code == 201
    await _put(client, collection_id, "a.md")
    await _put(client, "kb-2", "keep.md")

    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 204

    assert [d.path for d in await _documents_of(provider, "kb-2")] == ["keep.md"]
    assert vectors.chunks_of("kb-2"), "deleting one collection removed another's chunks"


@pytest.mark.asyncio
async def test_a_collection_larger_than_one_batch_is_removed_whole(client, provider, collection_id, vectors):
    service = DocumentService(provider)
    paths = [f"bulk/{n:04d}.md" for n in range(450)]
    for path in paths:
        await service.upsert(collection_id=collection_id, path=path, content="x")
    assert len(await _documents_of(provider, collection_id)) == 450

    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 204

    assert await _documents_of(provider, collection_id) == [], "a document past the first batch survived"
    assert await _content_rows(provider, collection_id, paths) == []


@pytest.mark.asyncio
async def test_a_vector_store_that_cannot_be_reached_refuses_the_delete_and_changes_nothing(
    client, provider, collection_id, vectors,
):
    """Swallowing it would leave chunks that resurface under a reused id, so the delete stops before touching any row."""
    await _put(client, collection_id, "a.md", "b.md")
    vectors.drop_error = ConnectionError("the vector store is down")

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 502, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert collection_id in resp.json()["detail"] and "search" in resp.json()["detail"], "the refusal must say how to get unstuck"
    assert await provider.get_storage(Collection).get(collection_id) is not None
    assert len(await _documents_of(provider, collection_id)) == 2
    assert await _content_rows(provider, collection_id, ["a.md", "b.md"]) == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_after_search_is_disabled_a_collection_with_a_broken_vector_store_can_be_deleted(
    client, provider, collection_id, vectors,
):
    await _put(client, collection_id, "a.md")
    vectors.drop_error = ConnectionError("the vector store is down")
    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 502

    assert (await client.delete(f"/v1/collections/{collection_id}/search")).status_code == 204
    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert await _documents_of(provider, collection_id) == []


@pytest.mark.asyncio
async def test_a_vector_store_provider_that_no_longer_exists_does_not_block_the_delete(
    app, client, provider, collection_id, vectors,
):
    await _put(client, collection_id, "a.md")
    app.state.semantic_search_registry.get_store = AsyncMock(side_effect=NotFoundError("provider 'ssp-test' does not exist"))

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert await _documents_of(provider, collection_id) == []


@pytest.mark.asyncio
async def test_a_collection_without_search_is_deleted_without_asking_any_vector_store(client, provider, app, vectors):
    assert (await client.post("/v1/collections", json=Collection(id="plain", description="p").model_dump(mode="json"))).status_code == 201
    await _put(client, "plain", "a.md")
    app.state.semantic_search_registry.get_store.reset_mock()

    resp = await client.delete("/v1/collections/plain")

    assert resp.status_code == 204, resp.text
    assert await _documents_of(provider, "plain") == []
    app.state.semantic_search_registry.get_store.assert_not_called()


@pytest.mark.asyncio
async def test_a_harness_managed_collection_is_refused_and_nothing_is_cascaded(client, provider, vectors):
    """The managed-by guard runs first, so a refused delete must not have emptied the collection on its way."""
    managed = Collection(id="owned", description="owned by a harness", harness_id="hns_x")
    await provider.get_storage(Collection).create(managed)
    await DocumentService(provider).upsert(collection_id="owned", path="a.md", content="keep me")

    resp = await client.delete("/v1/collections/owned")

    assert resp.status_code in (403, 409), resp.text
    assert await provider.get_storage(Collection).get("owned") is not None
    assert [d.path for d in await _documents_of(provider, "owned")] == ["a.md"]
    assert await _content_rows(provider, "owned", ["a.md"]) == ["a.md"]


@pytest.mark.asyncio
async def test_a_system_collection_cannot_be_deleted_and_nothing_is_cascaded(client, provider, vectors):
    """The model promises a system collection is read-only through every path; the generic delete was the one that did not keep it
    (RUN: 204), and with the cascade it would now also have emptied it. It is regenerated from platform state, not edited by hand."""
    await provider.get_storage(Collection).create(Collection(id="sys-1", description="system", system=True))
    await DocumentService(provider).upsert(collection_id="sys-1", path="map.md", content="regenerated from platform state")

    resp = await client.delete("/v1/collections/sys-1")

    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "system-owned and read-only" in resp.json()["detail"]
    assert await provider.get_storage(Collection).get("sys-1") is not None
    assert [d.path for d in await _documents_of(provider, "sys-1")] == ["map.md"]


def _record_order(monkeypatch, provider, vectors) -> list[str]:
    """Log, in call order, every step that removes something: the vector drop, each document, each content row, the collection row."""
    log: list[str] = []

    def around(label, real):
        async def wrapper(*args, **kwargs):
            log.append(label)
            return await real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(vectors, "drop_collection", around("vectors", vectors.drop_collection))
    docs, colls = provider.get_storage(Document), provider.get_storage(Collection)
    monkeypatch.setattr(docs, "delete", around("document", docs.delete))
    monkeypatch.setattr(colls, "delete", around("row", colls.delete))
    real_content = SqliteDocumentContentStore.delete

    async def content_delete(self, *args, **kwargs):
        log.append("content")
        return await real_content(self, *args, **kwargs)

    monkeypatch.setattr(SqliteDocumentContentStore, "delete", content_delete)
    real_sweep = SqliteDocumentContentStore.delete_collection

    async def content_sweep(self, *args, **kwargs):
        log.append("sweep")
        return await real_sweep(self, *args, **kwargs)

    monkeypatch.setattr(SqliteDocumentContentStore, "delete_collection", content_sweep)
    return log


@pytest.mark.asyncio
async def test_the_cascade_runs_before_the_collection_row_is_deleted(client, provider, collection_id, vectors, monkeypatch):
    """A failure part-way must leave a collection that deleting again finishes, never documents without a collection: the row goes last."""
    await _put(client, collection_id, "a.md", "b.md")
    log = _record_order(monkeypatch, provider, vectors)

    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 204

    assert log.count("row") == 1 and log[-1] == "row", f"the row was not the last thing removed: {log}"
    assert log[0] == "vectors", f"the vector namespace was not dropped first: {log}"
    assert log.count("document") == 2 and log.count("content") == 2
    assert log.count("sweep") == 1 and log.index("sweep") == len(log) - 2, f"the content sweep is not the step just before the row: {log}"


@pytest.mark.asyncio
async def test_chunks_written_by_an_indexing_pass_that_lands_mid_purge_are_dropped_too(
    client, provider, collection_id, vectors, monkeypatch,
):
    """A PUT whose indexing pass was in flight when the namespace was dropped recreates the namespace and its chunks after the drop;
    left alone they are searchable orphans under an id that is about to be free."""
    await _put(client, collection_id, "a.md")
    docs = provider.get_storage(Document)
    real, landed = docs.delete, []

    async def indexing_lands(id, *, conn=None):
        if not landed:
            landed.append(id)
            vectors.namespaces.add(collection_id)
            vectors.records.append(SimpleNamespace(collection_id=collection_id, document_id=id, chunk_id="late"))
        return await real(id, conn=conn)

    monkeypatch.setattr(docs, "delete", indexing_lands)

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert landed, "the scenario never ran"
    assert vectors.chunks_of(collection_id) == [], "a chunk written mid-purge survived the delete"
    assert collection_id not in vectors.namespaces
    assert vectors.drop_calls >= 2, "the namespace was dropped only before the documents, not after"


@pytest.mark.asyncio
async def test_a_document_created_while_the_namespace_is_dropped_again_is_removed_too(client, provider, collection_id, vectors):
    """The documents are re-checked just before the row goes, so a write that lands after the loop does not become an orphan."""
    await _put(client, collection_id, "a.md")
    created: list[int] = []

    async def late_write(n):
        if n == 2:
            created.append(n)
            await DocumentService(provider).upsert(collection_id=collection_id, path="late.md", content="arrived late")

    vectors.on_drop = late_write

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert created, "the namespace was dropped only once, so nothing re-checked the documents"
    assert await _documents_of(provider, collection_id) == [], "a document written during the delete outlived its collection"
    assert await _content_rows(provider, collection_id, ["a.md", "late.md"]) == []


@pytest.mark.asyncio
async def test_a_collection_that_keeps_receiving_documents_is_not_deleted_and_says_so(client, provider, collection_id, vectors):
    """Bounded: the purge does not chase a writer for ever. It stops with a 409 that names the way out, and the row is still there."""
    await _put(client, collection_id, "a.md")

    async def writer_that_never_stops(n):
        if n >= 2:
            await DocumentService(provider).upsert(collection_id=collection_id, path=f"late-{n}.md", content="again")

    vectors.on_drop = writer_that_never_stops

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 409, resp.text
    assert collection_id in resp.json()["detail"]
    assert vectors.drop_calls <= 6, "the purge chased the writer without a bound"
    assert await provider.get_storage(Collection).get(collection_id) is not None


@pytest.mark.asyncio
async def test_a_delete_that_removes_nothing_is_a_server_error_not_a_vector_store_fault(
    client, provider, collection_id, vectors, monkeypatch,
):
    await _put(client, collection_id, "a.md")

    async def removes_nothing(id, *, conn=None):
        return None

    monkeypatch.setattr(provider.get_storage(Document), "delete", removes_nothing)

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 500, f"a stuck document delete reads as {resp.status_code}: {resp.text}"
    assert resp.json()["type"].endswith("/errors/internal"), resp.json()
    assert await provider.get_storage(Collection).get(collection_id) is not None


@pytest.mark.asyncio
async def test_a_document_deleted_concurrently_counts_as_already_deleted(client, provider, collection_id, vectors, monkeypatch):
    """Another request deleting a document between the page read and the purge's own delete is the outcome the purge wants, not a 404."""
    await _put(client, collection_id, "a.md", "b.md")
    docs = provider.get_storage(Document)
    real, raced = docs.delete, []

    async def deleted_by_someone_else_first(id, *, conn=None):
        if not raced:
            raced.append(id)
            await real(id)
        return await real(id, conn=conn)

    monkeypatch.setattr(docs, "delete", deleted_by_someone_else_first)

    resp = await client.delete(f"/v1/collections/{collection_id}")

    assert resp.status_code == 204, resp.text
    assert raced, "the scenario never ran"
    assert await _documents_of(provider, collection_id) == []
    assert await _content_rows(provider, collection_id, ["a.md", "b.md"]) == []


@pytest.mark.asyncio
async def test_content_rows_with_no_document_entity_are_removed_with_the_collection(client, provider, collection_id, vectors):
    """UNIQUE(collection_id, path) would make such a row block the path in a collection created later under the same id."""
    content = provider.get_content_store()
    await content.upsert(document_id="ghost-1", collection_id=collection_id, path="ghost.md", content="no entity row")
    await content.upsert(document_id="ghost-2", collection_id="elsewhere", path="ghost.md", content="another collection's")

    assert (await client.delete(f"/v1/collections/{collection_id}")).status_code == 204

    assert await content.resolve_id(collection_id, "ghost.md") is None, "an entity-less content row outlived its collection"
    assert await content.resolve_id("elsewhere", "ghost.md") == "ghost-2", "another collection's content row was removed"


@pytest.mark.asyncio
async def test_a_failure_part_way_leaves_a_collection_that_deleting_again_finishes(
    client, provider, collection_id, vectors, monkeypatch,
):
    """Declared state after a failed delete: the vector namespace is already gone (so search on the collection finds nothing until the
    retry), the batches that committed are gone, the rest and the row remain, and a second DELETE finishes the job."""
    monkeypatch.setattr("primer.knowledge.lifecycle._PURGE_BATCH", 2)
    await _put(client, collection_id, *[f"d{n}.md" for n in range(5)])
    assert vectors.chunks_of(collection_id)
    docs = provider.get_storage(Document)
    real, calls = docs.delete, []

    async def fails_in_the_second_batch(id, *, conn=None):
        calls.append(id)
        if len(calls) == 3:
            raise PrimerError("the disk is full")
        return await real(id, conn=conn)

    monkeypatch.setattr(docs, "delete", fails_in_the_second_batch)

    failed = await client.delete(f"/v1/collections/{collection_id}")

    assert failed.status_code == 500, failed.text
    assert await provider.get_storage(Collection).get(collection_id) is not None
    assert len(await _documents_of(provider, collection_id)) == 3, "the first batch committed and the second rolled back"
    assert vectors.chunks_of(collection_id) == [], "search on the collection is empty until the retry"

    monkeypatch.setattr(docs, "delete", real)
    retried = await client.delete(f"/v1/collections/{collection_id}")

    assert retried.status_code == 204, retried.text
    assert await _documents_of(provider, collection_id) == []


@pytest.mark.asyncio
async def test_the_delete_route_documents_the_refusals_a_client_can_get(app):
    """403 (a system collection), 409 (managed, referenced or still being written) and 502 (an unreachable vector store) are answers the
    route gives; the generated spec said only 404 and 500."""
    spec = app.openapi()["paths"]["/v1/collections/{entity_id}"]

    assert {"403", "404", "409", "500", "502"} <= set(spec["delete"]["responses"]), sorted(spec["delete"]["responses"])
    assert "403" in spec["put"]["responses"], "the system-flag refusal is not documented on the update"
    assert "403" in app.openapi()["paths"]["/v1/collections"]["post"]["responses"], "nor on the create"
