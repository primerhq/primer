"""Tests for user-document chunking + embedding + indexing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.knowledge.indexing import (
    backfill_missing_document_vectors,
    chunk_text,
    index_document,
)
from primer.model.collection import Collection, CollectionEmbedder, Document
from primer.model.except_ import ConflictError, DimensionMismatchError, PrimerError
from primer.model.storage import OffsetPage, OffsetPageResponse


def _collection(system: bool = False) -> Collection:
    return Collection(
        id="kb-1",
        description="test",
        embedder=CollectionEmbedder(provider_id="emb", model="m"),
        search_provider_id="ssp",
        system=system,
    )


def _document(text: str | None = None, content: str | None = None) -> Document:
    meta = {}
    if text is not None:
        meta["text"] = text
    if content is not None:
        meta["content"] = content
    return Document(id="doc-1", collection_id="kb-1", name="d", meta=meta)


class TestChunkText:
    def test_empty_returns_no_chunks(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_is_one_chunk(self):
        assert chunk_text("hello world") == ["hello world"]

    def test_paragraphs_pack_greedily(self):
        text = "\n\n".join(["a" * 800, "b" * 800, "c" * 800])
        chunks = chunk_text(text)
        # 800 + 2 + 800 = 1602 > 1500, so each 800-char paragraph is its
        # own chunk.
        assert len(chunks) == 3

    def test_overlong_paragraph_is_hard_split(self):
        text = "x" * 5000
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        assert all(len(c) <= 1500 for c in chunks)


class _Emb:
    def __init__(self, dim: int = 3):
        self._dim = dim

    async def embed(self, *, model, inputs):
        class _R:
            embeddings = [type("V", (), {"vector": [0.1] * self._dim})()]
        return _R()


class _Store:
    def __init__(self):
        self.created = None
        self.puts = []
        self.deleted = []
        self._registered: set[str] = set()

    async def delete(self, cid, did):
        self.deleted.append((cid, did))

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.created = (cid, dimensions)
        self._registered.add(cid)

    async def put(self, record):
        self.puts.append(record)

    async def search_by_meta(self, cid, *, meta):
        # Mirror the real store: an unregistered collection raises rather
        # than returning an empty list.
        if cid not in self._registered:
            raise PrimerError(f"collection {cid!r} is not registered")
        return [r for r in self.puts if r.collection_id == cid]


class TestIndexDocument:
    @pytest.mark.asyncio
    async def test_indexes_chunks_with_embeddings(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        # Two 800-char paragraphs exceed the 1500-char target when packed
        # together, so they become two chunks.
        n = await index_document(
            document=_document(text="\n\n".join(["a" * 800, "b" * 800])),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 2
        assert store.created == ("kb-1", 4)
        assert len(store.puts) == 2
        assert store.puts[0].document_id == "doc-1"
        assert store.puts[0].chunk_id == "0"
        assert store.puts[1].chunk_id == "1"
        assert len(store.puts[0].vector) == 4
        # Re-index clears old chunks first.
        assert ("kb-1", "doc-1") in store.deleted

    @pytest.mark.asyncio
    async def test_system_collection_skipped(self):
        reg = AsyncMock()
        ssr = AsyncMock()
        n = await index_document(
            document=_document(text="anything"),
            collection=_collection(system=True),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 0
        ssr.get_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_key_fallback(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=_document(content="from content key"),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        assert store.puts[0].text == "from content key"

    @pytest.mark.asyncio
    async def test_empty_document_clears_but_indexes_nothing(self):
        store = _Store()
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=_document(text=""),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 0
        # The dim-mismatch probe registers the collection (dim=3) even for
        # empty documents so a subsequent non-empty ingest gets the same
        # registration path and a mismatch surfaces early.
        assert store.created == ("kb-1", 3)
        assert len(store.puts) == 0  # no chunks stored
        assert ("kb-1", "doc-1") in store.deleted  # old chunks cleared


class _StatefulStore:
    """Vector store that models real searchable state: delete removes rows,
    put upserts on (document_id, chunk_id). Lets us assert what survives a
    failed re-index."""

    def __init__(self):
        self._rows: dict[tuple, object] = {}
        self._registered: set[str] = set()

    async def delete(self, cid, did):
        for key in [k for k in self._rows if k[0] == cid and k[1] == did]:
            del self._rows[key]

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self._registered.add(cid)

    async def put(self, record):
        self._rows[(record.collection_id, record.document_id, record.chunk_id)] = record

    async def get(self, cid, did):
        return [r for k, r in sorted(self._rows.items()) if k[0] == cid and k[1] == did]


class _RaisingEmb:
    """Embedder that raises, simulating a transient embedder/network error."""

    async def embed(self, *, model, inputs):
        raise PrimerError("transient embedder failure")


class TestReindexFailureKeepsOldChunks:
    @pytest.mark.asyncio
    async def test_failed_reembed_does_not_delete_old_chunks(self):
        store = _StatefulStore()
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        # First successful index: two chunks land and are searchable.
        ok_reg = AsyncMock()
        ok_reg.get_embedder = AsyncMock(return_value=_Emb(dim=3))
        await index_document(
            document=_document(text="\n\n".join(["a" * 800, "b" * 800])),
            collection=_collection(),
            provider_registry=ok_reg,
            semantic_search_registry=ssr,
        )
        before = await store.get("kb-1", "doc-1")
        assert len(before) == 2

        # Re-index with an embedder that raises mid-pipeline.
        bad_reg = AsyncMock()
        bad_reg.get_embedder = AsyncMock(return_value=_RaisingEmb())
        with pytest.raises(PrimerError):
            await index_document(
                document=_document(text="\n\n".join(["a" * 800, "b" * 800])),
                collection=_collection(),
                provider_registry=bad_reg,
                semantic_search_registry=ssr,
            )

        # The old chunks must still be present/searchable.
        after = await store.get("kb-1", "doc-1")
        assert len(after) == 2, "failed re-index destroyed the old chunks"

    @pytest.mark.asyncio
    async def test_successful_reindex_fully_replaces(self):
        store = _StatefulStore()
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb(dim=3))

        # Index a 3-chunk doc.
        await index_document(
            document=_document(text="\n\n".join(["a" * 800, "b" * 800, "c" * 800])),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert len(await store.get("kb-1", "doc-1")) == 3

        # Re-index with a shorter body -> stale chunks must be gone.
        await index_document(
            document=_document(text="just one short chunk"),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        final = await store.get("kb-1", "doc-1")
        assert len(final) == 1
        assert final[0].text == "just one short chunk"


class _DocStore:
    """Minimal Storage[Document] supporting get + paginated list."""

    def __init__(self, docs):
        self._docs = {d.id: d for d in docs}

    async def get(self, id):
        return self._docs.get(id)

    async def list(self, page, *, order_by=None):
        items = list(self._docs.values())
        sliced = items[page.offset:page.offset + page.length]
        return OffsetPageResponse(
            offset=page.offset, length=len(sliced), total=len(items), items=sliced,
        )


class _CollStore:
    def __init__(self, collections):
        self._c = {c.id: c for c in collections}

    async def get(self, id):
        return self._c.get(id)


class _StorageProvider:
    def __init__(self, docs, collections):
        self._doc_store = _DocStore(docs)
        self._coll_store = _CollStore(collections)

    def get_storage(self, model_cls):
        if model_cls is Document:
            return self._doc_store
        if model_cls is Collection:
            return self._coll_store
        raise AssertionError(f"unexpected model {model_cls!r}")


class TestBackfill:
    @pytest.mark.asyncio
    async def test_indexes_only_unindexed_documents(self):
        store = _Store()
        # doc-a is already indexed; doc-b is not.
        store._registered.add("kb-1")
        from primer.model.vector import EmbeddingRecord

        store.puts.append(
            EmbeddingRecord(
                collection_id="kb-1", document_id="doc-a", chunk_id="0",
                text="x", vector=[0.1, 0.2, 0.3], meta={},
            )
        )
        doc_a = Document(id="doc-a", collection_id="kb-1", name="a",
                         meta={"text": "already indexed"})
        doc_b = Document(id="doc-b", collection_id="kb-1", name="b",
                         meta={"text": "needs indexing"})
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)
        sp = _StorageProvider([doc_a, doc_b], [_collection()])

        n = await backfill_missing_document_vectors(
            storage_provider=sp,
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        # Only doc-b got embedded (its chunk was put after the pre-seeded one).
        new_puts = [p for p in store.puts if p.document_id == "doc-b"]
        assert len(new_puts) == 1

    @pytest.mark.asyncio
    async def test_unregistered_collection_indexes_all(self):
        store = _Store()  # nothing registered -> search_by_meta raises
        doc = Document(id="doc-1", collection_id="kb-1", name="d",
                       meta={"text": "hello"})
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)
        sp = _StorageProvider([doc], [_collection()])

        n = await backfill_missing_document_vectors(
            storage_provider=sp,
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        assert store.created == ("kb-1", 3)

    @pytest.mark.asyncio
    async def test_system_collection_skipped(self):
        doc = Document(id="doc-1", collection_id="sys", name="d",
                       meta={"text": "hello"})
        sys_coll = Collection(
            id="sys", description="t",
            embedder=CollectionEmbedder(provider_id="emb", model="m"),
            search_provider_id="ssp", system=True,
        )
        reg = AsyncMock()
        ssr = AsyncMock()
        sp = _StorageProvider([doc], [sys_coll])

        n = await backfill_missing_document_vectors(
            storage_provider=sp,
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 0
        ssr.get_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_bad_document_does_not_abort_others(self):
        store = _Store()
        doc_ok = Document(id="ok", collection_id="kb-1", name="ok",
                          meta={"text": "fine"})
        doc_bad = Document(id="bad", collection_id="kb-1", name="bad",
                           meta={"text": "boom"})

        class _FlakyEmb:
            async def embed(self, *, model, inputs):
                if "boom" in inputs[0].text:
                    raise PrimerError("embedder exploded")
                class _R:
                    embeddings = [type("V", (), {"vector": [0.1, 0.2, 0.3]})()]
                return _R()

        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_FlakyEmb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)
        sp = _StorageProvider([doc_bad, doc_ok], [_collection()])

        # Should not raise; the good doc still gets indexed.
        n = await backfill_missing_document_vectors(
            storage_provider=sp,
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        assert any(p.document_id == "ok" for p in store.puts)


class _MismatchStore(_Store):
    """Vector store that already has a collection registered at a DIFFERENT dim.

    ``create_collection`` raises ConflictError (matching the pgvector backend)
    when the requested dimension differs from the stored one.
    """

    def __init__(self, stored_dim: int, collection_id: str = "kb-1"):
        super().__init__()
        self._stored_dim = stored_dim
        self._stored_id = collection_id
        # Pre-register so the first create_collection raises.
        self._registered.add(collection_id)

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        if cid == self._stored_id and dimensions != self._stored_dim:
            raise ConflictError(
                f"collection {cid!r} already exists with "
                f"dimensions={self._stored_dim}, distance='cosine'; "
                f"requested dimensions={dimensions}, distance='cosine'"
            )
        await super().create_collection(cid, dimensions=dimensions, distance=distance)


class TestDimensionMismatchDetection:
    """DimensionMismatchError is raised BEFORE embedding any chunks."""

    @pytest.mark.asyncio
    async def test_mismatch_raises_before_embedding_chunks(self):
        """A 384-dim embedder against a 768-dim collection must raise 422 early."""
        store = _MismatchStore(stored_dim=768)
        embed_call_count = 0

        class _CountingEmb:
            async def embed(self, *, model, inputs):
                nonlocal embed_call_count
                embed_call_count += 1
                # Return 384-dim vectors (mismatch vs stored 768).
                class _R:
                    embeddings = [type("V", (), {"vector": [0.1] * 384})()]
                return _R()

        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_CountingEmb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        with pytest.raises(DimensionMismatchError) as exc_info:
            await index_document(
                document=_document(text="some text to index"),
                collection=_collection(),
                provider_registry=reg,
                semantic_search_registry=ssr,
            )

        err = exc_info.value
        assert err.embedder_dim == 384
        assert err.collection_dim == 768
        assert err.collection_id == "kb-1"
        # Only the probe embed ran -- no chunk embedding happened.
        assert embed_call_count == 1
        # No chunks were stored.
        assert store.puts == []

    @pytest.mark.asyncio
    async def test_matching_dims_proceeds_normally(self):
        """When embedder dim matches collection stored dim, indexing succeeds."""
        store = _MismatchStore(stored_dim=3)  # same as _Emb default
        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_Emb(dim=3))
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        n = await index_document(
            document=_document(text="short text"),
            collection=_collection(),
            provider_registry=reg,
            semantic_search_registry=ssr,
        )
        assert n == 1
        assert len(store.puts) == 1

    @pytest.mark.asyncio
    async def test_mismatch_error_carries_422_status(self):
        """DimensionMismatchError.status_code is 422."""
        store = _MismatchStore(stored_dim=768)

        class _384Emb:
            async def embed(self, *, model, inputs):
                class _R:
                    embeddings = [type("V", (), {"vector": [0.1] * 384})()]
                return _R()

        reg = AsyncMock()
        reg.get_embedder = AsyncMock(return_value=_384Emb())
        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=store)

        with pytest.raises(DimensionMismatchError) as exc_info:
            await index_document(
                document=_document(text="text"),
                collection=_collection(),
                provider_registry=reg,
                semantic_search_registry=ssr,
            )

        assert exc_info.value.status_code == 422
        assert "re-ingest" in exc_info.value.message.lower() or \
               "re-index" in exc_info.value.message.lower()
