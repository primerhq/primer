"""Deleting a collection deletes what it owns: its documents, their content rows and its vector chunks (ticket 01a1131f "F").

RUN through the system tool on real sqlite (D3 probe, scenario F): after ``delete_collection`` of a collection holding documents the
collection row was gone but every document, every content row (``UNIQUE(collection_id, path)``) and every vector chunk was still there,
and the chunks were still searchable. ``collection_router`` is a bare ``make_crud_router``; nothing cascaded. Worse, a collection created
later under the same id (ids are chosen by the caller) found the old documents and chunks waiting for it.

The cascade runs BEFORE the collection row is removed and in this order: the vector namespace (one ``drop_collection``, so the cost does not
grow with the collection), then the documents with their content rows in batches of one transaction each, then the row. It is synchronous:
collections are text-only and bounded (a 1 MiB cap per document), the vector side is one call, and the caller learns the outcome from the
response, as ``PUT .../search`` (the backfill) already does. A vector store that cannot be reached REFUSES the delete (502) and changes
nothing, because swallowing it would leave chunks that resurface under a reused id; a provider or namespace that is simply gone does not block
it. To delete a collection whose vector store is permanently broken, ``DELETE .../search`` first (it always works), then delete.

The tool side is pinned in ``tests/toolset/test_system_collection_delete.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.knowledge.document_service import DocumentService
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

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

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.namespaces.add(cid)

    async def put(self, record):
        self.records.append(record)

    async def get(self, cid, document_id):
        return [r for r in self.records if r.collection_id == cid and r.document_id == document_id]

    async def delete(self, cid, document_id):
        self.records = [r for r in self.records if not (r.collection_id == cid and r.document_id == document_id)]

    async def drop_collection(self, cid):
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
    assert vectors.dropped == [collection_id], "the collection's vector namespace was not dropped"
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
