"""The path-addressed document surface un-indexes on delete and rewrites chunk paths on move (ticket 01a1131f "DocumentService never un-indexes").

``DocumentService`` (``DELETE`` / ``POST .../move`` on ``/v1/collections/{id}/documents``, and the system toolset's ``put_document`` /
``get_document_content``) took an ``indexer`` but no unindexer and no path rewriter: ``unindex`` appeared nowhere in the module. Deleting a
document by path removed the entity and content rows and stopped, so the document kept being returned by collection search; moving one left
every chunk's ``meta.path`` pointing at the old path. ``DocumentTreeService`` (the ``/docs`` routes and the collections toolset) and the generic
``/v1/documents`` route already did both, so which answer a deleted document got depended on which door it was deleted through.

The service now takes the same best-effort unindexer and path rewriter, and every place that builds one passes them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.knowledge.document_service import DocumentService

# Re-export so pytest can resolve the sqlite-backed app, client and search-enabled collection fixtures.
from tests.api.test_knowledge_documents_by_path import app, client, collection_id, provider  # noqa: F401
from tests.knowledge.test_indexing import _Emb


class _Vectors:
    """A vector store that upserts by (collection, document, chunk) like a real one, and can be made to fail on delete."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], object] = {}
        self.delete_error: Exception | None = None
        self.delete_calls = 0

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        return None

    async def put(self, record):
        self.rows[(record.collection_id, record.document_id, record.chunk_id)] = record

    async def get(self, cid, document_id):
        return [r for k, r in self.rows.items() if k[0] == cid and k[1] == document_id]

    async def delete(self, cid, document_id):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        for key in [k for k in self.rows if k[0] == cid and k[1] == document_id]:
            del self.rows[key]

    async def search_by_meta(self, cid, meta=None):
        return [r for k, r in self.rows.items() if k[0] == cid]

    def chunks(self, cid: str, document_id: str | None = None) -> list:
        return [r for k, r in self.rows.items() if k[0] == cid and (document_id is None or k[1] == document_id)]


@pytest.fixture
def vectors(app) -> _Vectors:
    store = _Vectors()
    app.state.provider_registry.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    app.state.semantic_search_registry.get_store = AsyncMock(return_value=store)
    return store


def _docs_url(cid: str) -> str:
    return f"/v1/collections/{cid}/documents"


async def _put(client, cid: str, path: str) -> str:
    r = await client.put(_docs_url(cid), params={"path": path}, json={"content": f"the body of {path}"})
    assert r.status_code in (200, 201), r.text
    return r.json()["document"]["id"]


@pytest.mark.asyncio
async def test_deleting_a_document_by_path_removes_its_chunks(client, collection_id, vectors):
    doc_id = await _put(client, collection_id, "a.md")
    assert vectors.chunks(collection_id, doc_id), "precondition: the document was indexed"

    resp = await client.delete(_docs_url(collection_id), params={"path": "a.md"})

    assert resp.status_code == 204, resp.text
    assert vectors.chunks(collection_id, doc_id) == [], "a deleted document is still searchable"


@pytest.mark.asyncio
async def test_deleting_one_document_leaves_the_other_documents_chunks(client, collection_id, vectors):
    gone = await _put(client, collection_id, "gone.md")
    kept = await _put(client, collection_id, "kept.md")

    assert (await client.delete(_docs_url(collection_id), params={"path": "gone.md"})).status_code == 204

    assert vectors.chunks(collection_id, gone) == []
    assert vectors.chunks(collection_id, kept), "deleting one document removed another's chunks"


@pytest.mark.asyncio
async def test_moving_a_document_rewrites_the_path_in_its_chunks(client, collection_id, vectors):
    doc_id = await _put(client, collection_id, "old/name.md")
    assert {r.meta["path"] for r in vectors.chunks(collection_id, doc_id)} == {"old/name.md"}

    moved = await client.post(f"/v1/collections/{collection_id}/documents/move", json={"from": "old/name.md", "to": "new/place.md"})

    assert moved.status_code == 204, moved.text
    paths = {r.meta["path"] for r in vectors.chunks(collection_id, doc_id)}
    assert paths == {"new/place.md"}, f"search would still show the old path: {paths}"


@pytest.mark.asyncio
async def test_a_vector_store_that_fails_to_unindex_does_not_fail_the_delete(client, provider, collection_id, vectors):
    """Best-effort, as for the tree service: the rows are the truth and are already gone; the failure is logged."""
    await _put(client, collection_id, "a.md")
    vectors.delete_error = ConnectionError("the vector store is down")
    vectors.delete_calls = 0  # indexing the body cleared stale chunks once already; count only what the DELETE asks

    resp = await client.delete(_docs_url(collection_id), params={"path": "a.md"})

    assert resp.status_code == 204, resp.text
    assert await provider.get_content_store().resolve_id(collection_id, "a.md") is None
    assert vectors.delete_calls == 1, "the delete never asked the vector store to drop the document's chunks"


@pytest.mark.asyncio
async def test_deleting_a_document_in_a_collection_without_search_asks_no_vector_store(app, client, vectors):
    from primer.model.collection import Collection

    assert (await client.post("/v1/collections", json=Collection(id="plain", description="p").model_dump(mode="json"))).status_code == 201
    await _put(client, "plain", "a.md")
    app.state.semantic_search_registry.get_store.reset_mock()

    resp = await client.delete(_docs_url("plain"), params={"path": "a.md"})

    assert resp.status_code == 204, resp.text
    app.state.semantic_search_registry.get_store.assert_not_called()


@pytest.mark.asyncio
async def test_a_service_built_without_the_hooks_still_deletes_and_moves(provider):
    """Search-off configurations (unit tests, tools without a registry) pass neither hook."""
    service = DocumentService(provider)
    await service.upsert(collection_id="kb-x", path="a.md", content="x")

    await service.move(collection_id="kb-x", src="a.md", dst="b.md")
    await service.delete(collection_id="kb-x", path="b.md")

    assert await provider.get_content_store().resolve_id("kb-x", "b.md") is None
