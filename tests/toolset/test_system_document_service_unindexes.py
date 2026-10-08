"""The system toolset's ``DocumentService`` un-indexes on delete and rewrites chunk paths on move (ticket 01a1131f).

The tool half of ``tests/api/test_document_path_service_unindexes.py``. ``_document_service_factory`` built the service with an inline indexer and
nothing else, so a path-addressed delete or move through it left the vector store as it was. Today the system tools reach it for ``put_document``
and the content reads only (their delete goes through the tree service), so this pins the factory, not a tool: anything that later calls
``delete`` / ``move`` on it must not reintroduce the hole.
"""

from __future__ import annotations

import pytest

from primer.toolset._system_crud import _document_service_factory

# Re-export so pytest can resolve the real-sqlite system toolset with a search-enabled collection ("kb-1") and a recording vector store.
from tests.toolset.test_system_document_tools import _put, _SSR, world  # noqa: F401


def _service(sp, store):
    return _document_service_factory(storage_provider=sp, provider_registry=None, semantic_search_registry=_SSR(store))()


def _chunks_of(store, document_id: str) -> list:
    return [r for k, r in store.records.items() if k[1] == document_id]


@pytest.mark.asyncio
async def test_deleting_through_the_toolsets_document_service_removes_the_chunks(world) -> None:
    sp, toolset, store = world
    doc_id = await _put(toolset, "notes/a.md")
    assert _chunks_of(store, doc_id), "precondition: put_document indexed the body"

    await _service(sp, store).delete(collection_id="kb-1", path="notes/a.md")

    assert _chunks_of(store, doc_id) == [], "a deleted document is still searchable"


@pytest.mark.asyncio
async def test_moving_through_the_toolsets_document_service_rewrites_the_chunk_paths(world) -> None:
    sp, toolset, store = world
    doc_id = await _put(toolset, "notes/a.md")

    async def get(collection_id, document_id):
        return [r for k, r in store.records.items() if k[0] == collection_id and k[1] == document_id]

    store.get = get  # the rewriter reads a document's chunks before putting them back with the new path
    await _service(sp, store).move(collection_id="kb-1", src="notes/a.md", dst="notes/b.md")

    assert {r.meta["path"] for r in _chunks_of(store, doc_id)} == {"notes/b.md"}
