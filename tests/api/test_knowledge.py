"""Phase-3 router tests: Collection + Document."""

from __future__ import annotations

import pytest

from primer.model.collection import Collection, CollectionEmbedder, Document


_SSP_BODY = {
    "id": "ssp-test",
    "provider": "pgvector",
    "config": {
        "hostname": "localhost",
        "port": 5432,
        "database": "primer",
        "username": "primer",
        "password": "primer",
        "db_schema": "public",
    },
}


def _collection(**overrides) -> Collection:
    body = dict(
        id="kb-1",
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
        search_provider_id="ssp-test",
    )
    body.update(overrides)
    return Collection(**body)


def _document(**overrides) -> Document:
    body = dict(
        id="doc-1",
        collection_id="kb-1",
        name="hello.txt",
        meta={},
    )
    body.update(overrides)
    return Document(**body)


class TestCollectionRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        body = _collection().model_dump(mode="json")
        post = await client.post("/v1/collections", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/collections/kb-1")
        assert get.status_code == 200

    @pytest.mark.asyncio
    async def test_list_collection_documents_works_when_collection_exists(
        self, client
    ) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        await client.post("/v1/collections", json=_collection().model_dump(mode="json"))
        await client.post(
            "/v1/documents",
            json=_document(id="doc-1", collection_id="kb-1").model_dump(mode="json"),
        )

        resp = await client.get("/v1/collections/kb-1/documents")
        assert resp.status_code == 200
        # The fake _InMemoryStorage.find ignores predicates and returns
        # the full set; the route still passes through Storage.find,
        # so this test verifies the wiring rather than the predicate
        # evaluation (which is the backend's concern).
        assert resp.json()["length"] >= 1

    @pytest.mark.asyncio
    async def test_list_documents_404_when_collection_missing(self, client) -> None:
        resp = await client.get("/v1/collections/missing/documents")
        assert resp.status_code == 404


class TestSearchUnregisteredCollection:
    """Search against a collection that exists but was never indexed in
    the vector store returns empty hits, not a 400. A user collection
    with Document rows but no vectorised chunks yet is the common case."""

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_not_registered(self):
        from unittest.mock import AsyncMock

        from primer.api.routers.knowledge import (
            _CollectionSearchBody,
            search_collection,
        )
        from primer.model.except_ import BadRequestError as _BRE

        coll = _collection(id="kb-unindexed")

        collections = AsyncMock()
        collections.get = AsyncMock(return_value=coll)

        # Embedder returns a one-vector response.
        class _Emb:
            async def embed(self, *, model, inputs):
                class _R:
                    embeddings = [type("V", (), {"vector": [0.1, 0.2, 0.3]})()]
                return _R()

        registry = AsyncMock()
        registry.get_embedder = AsyncMock(return_value=_Emb())

        # Store raises the lazy-registration error the way a real backend
        # does when nothing has been indexed for the collection yet.
        class _Store:
            async def search(self, cid, vector, top_k):
                raise _BRE(f"collection {cid!r} is not registered")

        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=_Store())

        result = await search_collection(
            collection_id="kb-unindexed",
            body=_CollectionSearchBody(query="anything", top_k=5),
            collections=collections,
            registry=registry,
            ssr=ssr,
        )
        assert result == {"hits": []}

    @pytest.mark.asyncio
    async def test_search_reraises_other_bad_requests(self):
        from unittest.mock import AsyncMock

        import pytest as _pytest

        from primer.api.routers.knowledge import (
            _CollectionSearchBody,
            search_collection,
        )
        from primer.model.except_ import BadRequestError as _BRE

        coll = _collection(id="kb-x")
        collections = AsyncMock()
        collections.get = AsyncMock(return_value=coll)

        class _Emb:
            async def embed(self, *, model, inputs):
                class _R:
                    embeddings = [type("V", (), {"vector": [0.1]})()]
                return _R()

        registry = AsyncMock()
        registry.get_embedder = AsyncMock(return_value=_Emb())

        class _Store:
            async def search(self, cid, vector, top_k):
                raise _BRE("dimension mismatch: expected 384 got 1")

        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=_Store())

        with _pytest.raises(_BRE):
            await search_collection(
                collection_id="kb-x",
                body=_CollectionSearchBody(query="q", top_k=5),
                collections=collections,
                registry=registry,
                ssr=ssr,
            )


class TestIndexedDocumentsDocumentIdFilter:
    """list_indexed_documents filters to a single document's chunks when
    document_id is supplied, backing the 'view chunks of a document' UI."""

    @pytest.mark.asyncio
    async def test_filters_by_document_id(self):
        from unittest.mock import AsyncMock

        from primer.api.routers.knowledge import list_indexed_documents

        coll = _collection(id="kb-chunks")
        collections = AsyncMock()
        collections.get = AsyncMock(return_value=coll)

        def _rec(doc_id, chunk_id):
            return type(
                "R", (), {
                    "document_id": doc_id,
                    "chunk_id": chunk_id,
                    "text": f"{doc_id}:{chunk_id}",
                    "meta": {},
                },
            )()

        records = [
            _rec("doc-a", "0"), _rec("doc-a", "1"),
            _rec("doc-b", "0"),
        ]

        class _Store:
            async def search_by_meta(self, cid, meta):
                return records

        ssr = AsyncMock()
        ssr.get_store = AsyncMock(return_value=_Store())

        # No filter: all 3 chunks.
        full = await list_indexed_documents(
            collection_id="kb-chunks", limit=50, offset=0,
            document_id=None, collections=collections, ssr=ssr,
        )
        assert full["total"] == 3

        # Filtered to doc-a: 2 chunks.
        scoped = await list_indexed_documents(
            collection_id="kb-chunks", limit=50, offset=0,
            document_id="doc-a", collections=collections, ssr=ssr,
        )
        assert scoped["total"] == 2
        assert {i["document_id"] for i in scoped["items"]} == {"doc-a"}


class TestSystemCollectionGuard:
    """Documents cannot be hand-ingested into system collections."""

    @pytest.mark.asyncio
    async def test_create_into_system_collection_rejected(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        sys_coll = _collection(id="_internal_test", system=True).model_dump(
            mode="json"
        )
        # System collections are normally created by internal subsystems;
        # create one directly through storage for the test by posting it.
        created = await client.post("/v1/collections", json=sys_coll)
        assert created.status_code == 201, created.text

        resp = await client.post(
            "/v1/documents",
            json=_document(id="doc-x", collection_id="_internal_test").model_dump(
                mode="json"
            ),
        )
        assert resp.status_code == 400, resp.text
        assert "system-managed" in resp.json().get("detail", "")

    @pytest.mark.asyncio
    async def test_create_into_user_collection_allowed(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        await client.post(
            "/v1/collections",
            json=_collection(id="kb-user").model_dump(mode="json"),
        )
        resp = await client.post(
            "/v1/documents",
            json=_document(id="doc-ok", collection_id="kb-user").model_dump(
                mode="json"
            ),
        )
        assert resp.status_code == 201, resp.text


class TestDocumentRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _document().model_dump(mode="json")
        post = await client.post("/v1/documents", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/documents/doc-1")
        assert get.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_document(self, client) -> None:
        """Documents are deletable through the standard CRUD DELETE route
        (the console exposes this as a per-row trash action)."""
        await client.post(
            "/v1/documents",
            json=_document(id="doc-del").model_dump(mode="json"),
        )
        delete = await client.delete("/v1/documents/doc-del")
        assert delete.status_code in (200, 204), delete.text
        gone = await client.get("/v1/documents/doc-del")
        assert gone.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_missing_document_404(self, client) -> None:
        resp = await client.delete("/v1/documents/nope")
        assert resp.status_code == 404


class TestConvertUploadedFile:
    """The /documents/_convert_file endpoint short-circuits docling for
    already-text formats (.md / .txt). Regression test for the upload
    failing on markdown source files."""

    @pytest.mark.asyncio
    async def test_markdown_extension_returns_text_verbatim(self, client):
        body = b"# Hello\n\nThis is *markdown*.\n"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("note.md", body, "text/markdown")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "note.md"
        assert data["bytes_loaded"] == len(body)
        assert data["text"] == body.decode("utf-8")

    @pytest.mark.asyncio
    async def test_plain_text_extension_returns_text_verbatim(self, client):
        body = b"plain text content"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("note.txt", body, "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "plain text content"

    @pytest.mark.asyncio
    async def test_markdown_via_content_type_only(self, client):
        """A file without a known extension but tagged
        text/markdown still goes through the passthrough path."""
        body = b"# heading\nbody\n"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("blob", body, "text/markdown")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == body.decode("utf-8")

    @pytest.mark.asyncio
    async def test_empty_file_rejected(self, client):
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_utf8_text_rejected_with_clear_message(self, client):
        # Latin-1 byte outside ASCII / UTF-8.
        body = b"\xff\xfe garbage"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("bad.md", body, "text/markdown")},
        )
        assert resp.status_code == 400
        assert "UTF-8" in resp.json().get("detail", "")
