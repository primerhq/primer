"""M5 / KN-15: a document write probes the embedder once per (collection, embedder), skips an unchanged document, and stores its chunks in one write."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from primer.int.vector_store import VectorStore
from primer.knowledge import indexing
from primer.knowledge.indexing import index_document, index_documents
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.vector import EmbeddingRecord


class _Embedder:
    def __init__(self):
        self.calls: list[int] = []  # inputs per call

    async def embed(self, *, model, inputs):
        self.calls.append(len(inputs))
        return SimpleNamespace(embeddings=[SimpleNamespace(vector=[0.1, 0.2, 0.3]) for _ in inputs])


class _Store(VectorStore):
    """Keeps records; counts what the indexing code asks of it."""

    def __init__(self):
        self.records: dict[tuple[str, str], EmbeddingRecord] = {}
        self.puts = 0
        self.put_many_calls = 0
        self.search_by_meta_calls = 0
        self.list_chunk_texts_calls = 0

    async def create_collection(self, collection_id, *, dimensions, distance="cosine"):
        return None

    async def put(self, record):
        self.puts += 1
        self.records[(record.document_id, record.chunk_id)] = record

    async def put_many(self, records):
        self.put_many_calls += 1
        for record in records:
            self.records[(record.document_id, record.chunk_id)] = record

    async def search(self, collection_id, vector, k):
        return []

    async def search_by_meta(self, collection_id, meta):
        self.search_by_meta_calls += 1
        return list(self.records.values())

    async def list_chunk_texts(self, collection_id, document_id=None):
        self.list_chunk_texts_calls += 1
        return [
            (r.document_id, r.chunk_id, r.text, r.meta.get("embedder")) for r in self.records.values() if document_id is None or r.document_id == document_id
        ]

    async def count_documents(self, collection_id):
        return len({r.document_id for r in self.records.values()})

    async def get(self, collection_id, document_id):
        return [r for r in self.records.values() if r.document_id == document_id]

    async def delete(self, collection_id, document_id):
        self.records = {k: v for k, v in self.records.items() if k[0] != document_id}

    async def drop_collection(self, collection_id):
        self.records.clear()


class _Content:
    def __init__(self, bodies: dict[str, str]):
        self.bodies = bodies

    async def get(self, document_id, *, conn=None):
        return self.bodies.get(document_id)


def _collection(cid="kb") -> Collection:
    return Collection(
        id=cid, description="d",
        search=CollectionSearchConfig(embedder=CollectionEmbedder(provider_id="emb", model="m"), vector_store_provider_id="ssp"),
    )


def _doc(did="d1") -> Document:
    return Document(id=did, collection_id="kb", slug=f"{did}.md", path=f"{did}.md", meta={})


def _body(n_chunks: int, tag="a") -> str:
    return "\n\n".join(f"{tag}{i} " + "x" * 1000 for i in range(n_chunks))  # 1000-char paragraphs: two never share a 1500-char chunk


def _wire(store, embedder):
    registry = SimpleNamespace(get_embedder=_async(embedder))
    ssr = SimpleNamespace(get_store=_async(store))
    return registry, ssr


def _async(value):
    async def get(*_a, **_k):
        return value

    return get


@pytest.fixture(autouse=True)
def _fresh_dimension_cache():
    indexing.forget_dimensions()
    yield
    indexing.forget_dimensions()


async def test_twelve_chunks_cost_one_embed_call_one_put_many_and_the_probe_only_once():
    store, embedder = _Store(), _Embedder()
    registry, ssr = _wire(store, embedder)
    content = _Content({"d1": _body(12)})
    n = await index_document(document=_doc(), collection=_collection(), provider_registry=registry, semantic_search_registry=ssr, content_store=content)
    assert n == 12 and len(store.records) == 12
    assert embedder.calls.count(1) == 1  # the dimensionality probe, once
    assert [c for c in embedder.calls if c != 1] == [12] and 12 <= 32  # ceil(12/32) == 1 embed call
    assert store.put_many_calls == 1 and store.puts == 0

    content.bodies["d1"] = _body(12, tag="b")  # a real edit
    await index_document(document=_doc(), collection=_collection(), provider_registry=registry, semantic_search_registry=ssr, content_store=content)
    assert embedder.calls.count(1) == 1  # no second probe for the same collection and embedder
    assert store.put_many_calls == 2 and math.ceil(12 / 32) == 1


async def test_an_unchanged_document_makes_no_embed_call_and_no_write():
    store, embedder = _Store(), _Embedder()
    registry, ssr = _wire(store, embedder)
    content = _Content({"d1": _body(5)})
    kwargs = dict(document=_doc(), collection=_collection(), provider_registry=registry, semantic_search_registry=ssr, content_store=content)
    first = await index_document(**kwargs)
    calls_before, writes_before = list(embedder.calls), store.put_many_calls
    second = await index_document(**kwargs)
    assert second == first == 5  # still reports its chunk count
    assert embedder.calls == calls_before and store.put_many_calls == writes_before


async def test_a_shorter_edit_still_drops_the_stale_chunks():
    store, embedder = _Store(), _Embedder()
    registry, ssr = _wire(store, embedder)
    content = _Content({"d1": _body(6)})
    kwargs = dict(document=_doc(), collection=_collection(), provider_registry=registry, semantic_search_registry=ssr, content_store=content)
    await index_document(**kwargs)
    content.bodies["d1"] = _body(2, tag="z")
    assert await index_document(**kwargs) == 2
    assert sorted(chunk for _, chunk in store.records) == ["0", "1"]


async def test_backfill_reads_chunk_texts_only_and_writes_once_per_document():
    store, embedder = _Store(), _Embedder()
    registry, ssr = _wire(store, embedder)
    docs = [_doc(f"d{i}") for i in range(10)]
    content = _Content({d.id: _body(5, tag=d.id) for d in docs})
    kwargs = dict(documents=docs, collection=_collection(), provider_registry=registry, semantic_search_registry=ssr, content_store=content)
    assert await index_documents(**kwargs) == 10
    assert store.search_by_meta_calls == 0 and store.list_chunk_texts_calls == 1  # no vectors decoded for the comparison
    # one batched write per finished document (KN-13: a document lands as soon as its vectors are in hand, so an interrupted backfill keeps it); never a per-chunk put
    assert store.put_many_calls == 10 and store.puts == 0
    calls = list(embedder.calls)
    assert await index_documents(**kwargs) == 0  # nothing drifted
    assert embedder.calls == calls and store.put_many_calls == 10


def test_the_default_put_many_is_a_loop_over_put():
    import asyncio

    class _Plain(_Store):
        put_many = VectorStore.put_many

    plain = _Plain()
    records = [EmbeddingRecord(collection_id="kb", document_id="d", chunk_id=str(i), text="t", vector=[0.0], meta={}) for i in range(3)]
    asyncio.run(plain.put_many(records))
    assert plain.puts == 3


def _collection_with(model: str, provider="emb") -> Collection:
    return Collection(
        id="kb", description="d",
        search=CollectionSearchConfig(embedder=CollectionEmbedder(provider_id=provider, model=model), vector_store_provider_id="ssp"),
    )


async def _index(store, embedder, content, collection, **extra):
    registry, ssr = _wire(store, embedder)
    return await index_document(
        document=_doc(), collection=collection, provider_registry=registry, semantic_search_registry=ssr, content_store=content, **extra,
    )


async def test_the_same_text_embedded_by_another_model_is_embedded_again():
    store, embedder = _Store(), _Embedder()
    content = _Content({"d1": _body(3)})
    await _index(store, embedder, content, _collection_with("model-a"))
    before = list(embedder.calls)
    await _index(store, embedder, content, _collection_with("model-b"))
    assert embedder.calls != before and any(c == 3 for c in embedder.calls[len(before):])
    assert {r.meta["embedder"] for r in store.records.values()} == {"emb|model-b"}
    # another provider with the same model name is a different embedder too
    await _index(store, embedder, content, _collection_with("model-b", provider="other"))
    assert {r.meta["embedder"] for r in store.records.values()} == {"other|model-b"}


async def test_the_same_text_by_the_same_model_is_skipped():
    store, embedder = _Store(), _Embedder()
    content = _Content({"d1": _body(3)})
    await _index(store, embedder, content, _collection_with("model-a"))
    calls = list(embedder.calls)
    await _index(store, embedder, content, _collection_with("model-a"))
    assert embedder.calls == calls


async def test_a_legacy_chunk_with_no_fingerprint_is_embedded_once_then_skipped():
    store, embedder = _Store(), _Embedder()
    content = _Content({"d1": _body(3)})
    collection = _collection_with("model-a")
    await _index(store, embedder, content, collection)
    for key, record in list(store.records.items()):  # chunks written before the fingerprint existed
        store.records[key] = record.model_copy(update={"meta": {"path": record.meta["path"]}})
    calls = list(embedder.calls)
    await _index(store, embedder, content, collection)
    assert len(embedder.calls) > len(calls)  # re-embedded once
    assert all(r.meta["embedder"] == "emb|model-a" for r in store.records.values())
    calls = list(embedder.calls)
    await _index(store, embedder, content, collection)
    assert embedder.calls == calls  # and now it is current


async def test_an_explicit_reindex_never_skips():
    store, embedder = _Store(), _Embedder()
    content = _Content({"d1": _body(3)})
    collection = _collection_with("model-a")
    await _index(store, embedder, content, collection)
    calls = list(embedder.calls)
    await _index(store, embedder, content, collection, force=True)
    assert len(embedder.calls) > len(calls)


async def test_backfill_re_embeds_after_a_model_change_and_force_re_embeds_everything():
    store, embedder = _Store(), _Embedder()
    registry, ssr = _wire(store, embedder)
    docs = [_doc(f"d{i}") for i in range(3)]
    content = _Content({d.id: _body(2, tag=d.id) for d in docs})

    def run(collection, **extra):
        return index_documents(
            documents=docs, collection=collection, provider_registry=registry, semantic_search_registry=ssr, content_store=content, **extra,
        )

    assert await run(_collection_with("model-a")) == 3
    assert await run(_collection_with("model-a")) == 0
    assert await run(_collection_with("model-b")) == 3  # same texts, new model: all re-embedded
    assert await run(_collection_with("model-b")) == 0
    assert await run(_collection_with("model-b"), force=True) == 3
