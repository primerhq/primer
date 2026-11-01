"""T59 (KN-7, KN-8, KN-9, KN-13, KN-19e): re-enabling search with another embedder or store re-indexes everything and drops the old namespace; a backfill
removes the chunks of documents that are gone; concurrent indexers of one document leave the latest body; a failing embedder keeps the documents already
done; an interrupted enable ends in `error`; orphan recovery removes the orphan's chunks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.knowledge.indexing import index_document, index_documents, remove_document_index
from primer.knowledge.lifecycle import enable_search, reset_interrupted_indexing
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.except_ import ConflictError
from primer.model.provider import (
    EmbeddingModel, EmbeddingProvider, EmbeddingProviderType, HuggingFaceConfig, Limits, SemanticSearchProvider, SqliteConfig,
    StorageProviderConfig, StorageProviderType,
)
from primer.model.vector import EmbeddingRecord
from primer.storage.factory import StorageProviderFactory


class _Emb:
    """Embeds every chunk to ``[tag] * dim``; ``gate`` stalls the embed of non-probe inputs; ``fail_on`` raises on a batch holding that text."""

    def __init__(self, dim=3, tag=0.1, gate=None, fail_on=None):
        self.dim, self.tag, self.gate, self.fail_on = dim, tag, gate, fail_on
        self.embedded: list[str] = []

    async def embed(self, *, model, inputs, config=None):
        texts = [getattr(i, "text", "") for i in inputs]
        if self.gate is not None and any(t != "dimensionality probe" for t in texts):
            await self.gate.wait()
        if self.fail_on is not None and self.fail_on in texts:
            raise RuntimeError("embedder down")
        self.embedded += [t for t in texts if t != "dimensionality probe"]
        vecs = [type("V", (), {"vector": [self.tag] * self.dim})() for _ in inputs]

        class _R:
            embeddings = vecs

        return _R()


class _Store:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], EmbeddingRecord] = {}
        self.dims: dict[str, int] = {}
        self.dropped: list[str] = []

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        if cid in self.dims and self.dims[cid] != dimensions:
            raise ConflictError(f"collection {cid!r} already exists with dimensions={self.dims[cid]}")
        self.dims[cid] = dimensions

    async def drop_collection(self, cid):
        self.dropped.append(cid)
        self.dims.pop(cid, None)
        self.rows = {k: v for k, v in self.rows.items() if k[0] != cid}

    async def delete(self, cid, did):
        for key in [k for k in self.rows if k[0] == cid and k[1] == did]:
            del self.rows[key]

    async def put(self, record):
        await asyncio.sleep(0)
        self.rows[(record.collection_id, record.document_id, record.chunk_id)] = record

    async def get(self, cid, did):
        return [r for k, r in sorted(self.rows.items()) if k[0] == cid and k[1] == did]

    async def search_by_meta(self, cid, meta):
        return [r for k, r in sorted(self.rows.items()) if k[0] == cid]


def _coll(model="m1", ssp="s"):
    return Collection(id="kb", description="kb", search=CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="emb-1", model=model), vector_store_provider_id=ssp))


def _regs(emb, store):
    reg = AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr = AsyncMock()
    ssr.get_store = AsyncMock(return_value=store)
    return reg, ssr


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = StorageProviderFactory.create(StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(id="kb", description="kb"))
    await provider.get_storage(EmbeddingProvider).create(EmbeddingProvider(
        id="emb-1", provider=EmbeddingProviderType.HUGGINGFACE, models=[EmbeddingModel(name="m1"), EmbeddingModel(name="m2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")), limits=Limits(max_concurrency=1),
    ))
    for ssp in ("s", "s2"):
        await provider.get_storage(SemanticSearchProvider).create(SemanticSearchProvider.model_validate(
            {"id": ssp, "provider": "pgvector",
             "config": {"hostname": "localhost", "port": 5432, "database": "p", "username": "p", "password": "p", "db_schema": "public"}}))
    yield provider
    await provider.aclose()


async def _docs(sp, *bodies):
    tree = DocumentTreeService(sp)
    return [await tree.create(collection_id="kb", parent="", slug=f"d{i}", body=body) for i, body in enumerate(bodies)]


# --- KN-7 ---------------------------------------------------------------------------------------------------------------------------------------

async def test_reenabling_with_a_new_embedder_re_embeds_everything_and_drops_the_old_namespace(sp):
    docs = await _docs(sp, "alpha", "beta")
    store = _Store()
    emb1, emb2 = _Emb(dim=3, tag=0.1), _Emb(dim=8, tag=0.9)
    reg, ssr = _regs(emb1, store)
    cfg1 = _coll("m1").search
    await enable_search(sp, reg, ssr, collection_id="kb", cfg=cfg1)
    assert {r.vector[0] for r in store.rows.values()} == {0.1}

    reg.get_embedder = AsyncMock(return_value=emb2)
    updated = await enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll("m2").search)

    assert updated.search.state == "ready", updated.search.error
    assert store.dropped == ["kb"]                                     # the old model's namespace went first
    assert {len(r.vector) for r in store.rows.values()} == {8} and {r.vector[0] for r in store.rows.values()} == {0.9}
    assert sorted(emb2.embedded) == ["alpha", "beta"] and len(docs) == 2


async def test_reenabling_with_the_same_config_is_a_no_op(sp):
    await _docs(sp, "alpha")
    store = _Store()
    emb = _Emb()
    reg, ssr = _regs(emb, store)
    await enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll("m1").search)
    before = list(emb.embedded)
    await enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll("m1").search)
    assert emb.embedded == before and store.dropped == []


async def test_moving_to_another_vector_store_drops_the_namespace_in_the_old_one(sp):
    await _docs(sp, "alpha")
    old, new = _Store(), _Store()
    reg = AsyncMock()
    reg.get_embedder = AsyncMock(return_value=_Emb())
    ssr = AsyncMock()
    ssr.get_store = AsyncMock(side_effect=lambda ssp_id: old if ssp_id == "s" else new)
    await enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll("m1", "s").search)
    await enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll("m1", "s2").search)
    assert old.dropped == ["kb"] and old.rows == {} and len(new.rows) == 1


# --- KN-8 ---------------------------------------------------------------------------------------------------------------------------------------

async def test_a_backfill_removes_the_chunks_of_documents_that_are_gone(sp):
    keep, gone = await _docs(sp, "keep me", "delete me")
    store = _Store()
    reg, ssr = _regs(_Emb(), store)
    kwargs = dict(collection=_coll(), provider_registry=reg, semantic_search_registry=ssr, content_store=sp.get_content_store())
    await index_documents(documents=[keep, gone], **kwargs)
    await DocumentTreeService(sp).delete(collection_id="kb", path="d1")
    await index_documents(documents=[keep], **kwargs)
    assert {k[1] for k in store.rows} == {keep.id}


# --- KN-9 ---------------------------------------------------------------------------------------------------------------------------------------

async def test_concurrent_indexers_of_one_document_leave_the_latest_body(sp):
    (doc,) = await _docs(sp, "v1")
    store = _Store()
    gate = asyncio.Event()
    reg_slow, ssr = _regs(_Emb(tag=0.1, gate=gate), store)
    kwargs = dict(document=doc, collection=_coll(), semantic_search_registry=ssr, content_store=sp.get_content_store(), dimensions=3)
    task_a = asyncio.create_task(index_document(provider_registry=reg_slow, **kwargs))
    await asyncio.sleep(0.05)                                           # A has read v1 and is stalled in the embedder
    await sp.get_content_store().upsert(document_id=doc.id, collection_id="kb", path="d0", content="v2")
    reg_fast, _ = _regs(_Emb(tag=0.2), store)
    task_b = asyncio.create_task(index_document(provider_registry=reg_fast, **kwargs))      # B waits for A's lock
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(task_a, task_b)                                  # A notices v2 after its embed and runs again; B then finds v2 current
    (rec,) = store.rows.values()
    assert rec.text == "v2"


async def test_a_delete_racing_an_index_leaves_no_chunks(sp):
    (doc,) = await _docs(sp, "body")
    store = _Store()
    gate = asyncio.Event()
    reg, ssr = _regs(_Emb(gate=gate), store)
    indexing = asyncio.create_task(index_document(
        document=doc, collection=_coll(), provider_registry=reg, semantic_search_registry=ssr, content_store=sp.get_content_store(), dimensions=3))
    await asyncio.sleep(0.05)
    await sp.get_content_store().delete(doc.id)                         # the document is deleted while its embed is in flight
    removal = asyncio.create_task(remove_document_index(document_id=doc.id, collection=_coll(), semantic_search_registry=ssr))
    gate.set()
    await asyncio.gather(indexing, removal)
    assert store.rows == {}


# --- KN-13 --------------------------------------------------------------------------------------------------------------------------------------

async def test_a_failing_last_batch_keeps_the_documents_already_indexed_and_the_rerun_embeds_only_the_rest(sp, monkeypatch):
    monkeypatch.setattr("primer.knowledge.indexing._EMBED_BATCH_SIZE", 1)        # one chunk per embedder call: the documents land in separate batches
    first, second = await _docs(sp, "first doc", "second doc")
    store = _Store()
    failing = _Emb(fail_on="second doc")
    reg, ssr = _regs(failing, store)
    kwargs = dict(documents=[first, second], collection=_coll(), semantic_search_registry=ssr, content_store=sp.get_content_store())
    with pytest.raises(RuntimeError):
        await index_documents(provider_registry=reg, **kwargs)
    assert {k[1] for k in store.rows} == {first.id}                     # the earlier document is indexed
    healthy = _Emb()
    reg2, _ = _regs(healthy, store)
    await index_documents(provider_registry=reg2, **kwargs)
    assert healthy.embedded == ["second doc"] and {k[1] for k in store.rows} == {first.id, second.id}


async def test_a_cancelled_enable_writes_error(sp):
    await _docs(sp, "alpha")
    store = _Store()
    gate = asyncio.Event()
    reg, ssr = _regs(_Emb(gate=gate), store)
    task = asyncio.create_task(enable_search(sp, reg, ssr, collection_id="kb", cfg=_coll().search))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await sp.get_storage(Collection).get("kb")
    assert stored.search is not None and stored.search.state == "error" and "interrupted" in (stored.search.error or "")


async def test_boot_turns_an_interrupted_indexing_state_into_error(sp):
    colls = sp.get_storage(Collection)
    row = await colls.get("kb")
    await colls.update(row.model_copy(update={"search": _coll().search.model_copy(update={"state": "indexing"})}))
    assert await reset_interrupted_indexing(sp) == ["kb"]
    stored = await colls.get("kb")
    assert stored.search.state == "error" and "re-enable" in stored.search.error
    assert await reset_interrupted_indexing(sp) == []


# --- KN-19e -------------------------------------------------------------------------------------------------------------------------------------

async def test_orphan_recovery_removes_the_orphans_chunks(sp):
    from primer.knowledge.indexing import make_document_unindexer
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, _write_tree_page

    colls = sp.get_storage(Collection)
    await colls.create(Collection(id=SYSTEM_COLLECTION_ID, description="wiki", system=True, search=_coll().search))
    store = _Store()
    store.rows[(SYSTEM_COLLECTION_ID, "orph", "0")] = EmbeddingRecord(
        collection_id=SYSTEM_COLLECTION_ID, document_id="orph", chunk_id="0", text="old", vector=[0.0])
    await sp.get_content_store().upsert(document_id="orph", collection_id=SYSTEM_COLLECTION_ID, path="page", content="old")   # no Document row
    _, ssr = _regs(_Emb(), store)
    tree = DocumentTreeService(sp, unindexer=make_document_unindexer(storage_provider=sp, semantic_search_registry=ssr))

    await _write_tree_page(tree, path="page", body="new", title="T", meta={})

    assert not any(key[1] == "orph" for key in store.rows)
