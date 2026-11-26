"""Deferred X2.4: honour the provider's distance_metric at the indexing call sites.

A collection that already exists keeps the metric stored at creation time
(the store refuses a different metric on recreate), so a pure
distance-metric drift must NOT break indexing: the indexing path keeps going
under the stored metric instead of surfacing the drift as a
DimensionMismatchError (the messages below are the two real backends'
ConflictError shapes). A dimension mismatch still refuses: pinned here so the
drift tolerance cannot grow into one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.int.vector_store import VectorStore
from primer.knowledge.indexing import index_document, index_documents
from primer.model.collection import (
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
    Document,
)
from primer.model.except_ import ConflictError, DimensionMismatchError

# The two real backends' ConflictError shapes for a pure metric drift (the
# stored dimension matches the requested one): pgvector names both metrics in
# one message, Lance raises a distance-only message (its dimension check
# runs first, so this one only fires when the dimensions already match).
PGVECTOR_DRIFT = (
    "collection 'kb-1' already exists with dimensions=4, distance='l2'; "
    "requested dimensions=4, distance='cosine'"
)
LANCE_DRIFT = (
    "collection 'kb-1' already exists with distance='cosine'; "
    "cannot recreate with distance='dot'"
)
# A real dimension mismatch: the stored 8 vs the requested 4.
DIMENSION_DRIFT = (
    "collection 'kb-1' already exists with dimensions=8, distance='l2'; "
    "requested dimensions=4, distance='l2'"
)


def _collection() -> Collection:
    search = CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="emb", model="m"),
        vector_store_provider_id="ssp",
    )
    return Collection(id="kb-1", description="test", search=search)


def _document() -> Document:
    return Document(
        id="doc-1", collection_id="kb-1", slug="doc-1.md", path="doc-1.md", meta={}
    )


class _ContentStore:
    def __init__(self, text: str) -> None:
        self._text = text

    async def get(self, document_id, *, conn=None):
        return self._text


class _Emb:
    """A 4-dim embedder: it matches the stored dimension in every message
    above, so only the metric can conflict."""

    async def embed(self, *, model, inputs):
        vecs = [type("V", (), {"vector": [0.1] * 4})() for _ in inputs]

        class _R:
            embeddings = vecs

        return _R()


class _DriftStore(VectorStore):
    """The collection already exists: ``create_collection`` raises the
    scripted ConflictError EVERY time (the probe's create and the
    post-embedding no-op re-adopt both hit the drift). Everything else
    mirrors the real store: replace_document runs for real on the puts."""

    def __init__(self, message: str) -> None:
        self._message = message
        self.puts = []

    async def delete(self, cid, did):
        self.puts = [r for r in self.puts if r.document_id != did]

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        raise ConflictError(self._message)

    async def put(self, record):
        self.puts.append(record)

    async def get(self, cid, did):
        return [r for r in self.puts if r.collection_id == cid and r.document_id == did]

    async def search(self, cid, vector, k):
        raise NotImplementedError

    async def count_documents(self, cid):
        return len({r.document_id for r in self.puts if r.collection_id == cid})

    async def drop_collection(self, cid):
        self.puts = [r for r in self.puts if r.collection_id != cid]

    async def search_by_meta(self, cid, meta=None):
        return [r for r in self.puts if r.collection_id == cid]


def _deps(store):
    reg = AsyncMock()
    reg.get_embedder = AsyncMock(return_value=_Emb())
    ssr = AsyncMock()
    ssr.get_store = AsyncMock(return_value=store)
    return reg, ssr


@pytest.mark.parametrize("message", [PGVECTOR_DRIFT, LANCE_DRIFT])
async def test_a_metric_drift_on_an_existing_collection_keeps_indexing(message):
    """index_document: the drift is not an error - the document is indexed
    under the collection's stored metric (the store re-resolves it)."""
    store = _DriftStore(message)
    reg, ssr = _deps(store)
    n = await index_document(
        document=_document(),
        collection=_collection(),
        provider_registry=reg,
        semantic_search_registry=ssr,
        content_store=_ContentStore("hello searchable world"),
    )
    assert n == 1
    assert len(store.puts) == 1, "the drift must not block the replace"
    assert store.puts[0].document_id == "doc-1"


async def test_a_dimension_mismatch_still_refuses_the_index():
    """The drift tolerance is metric-only: a stored-dimension mismatch is
    still a DimensionMismatchError carrying both dimensions."""
    store = _DriftStore(DIMENSION_DRIFT)
    reg, ssr = _deps(store)
    with pytest.raises(DimensionMismatchError) as ei:
        await index_document(
            document=_document(),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
            content_store=_ContentStore("hello searchable world"),
        )
    assert ei.value.embedder_dim == 4
    assert ei.value.collection_dim == 8


@pytest.mark.parametrize("message", [PGVECTOR_DRIFT, LANCE_DRIFT])
async def test_the_backfill_pass_tolerates_a_metric_drift_like_the_single_document_path(
    message,
):
    """index_documents (the backfill): the same drift is tolerated there."""
    store = _DriftStore(message)
    reg, ssr = _deps(store)
    n = await index_documents(
        documents=[_document()],
        collection=_collection(),
        provider_registry=reg,
        semantic_search_registry=ssr,
        content_store=_ContentStore("hello searchable world"),
    )
    assert n == 1
    assert len(store.puts) == 1
