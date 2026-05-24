"""Phase-3 router tests: Collection + Document.

VectorStoreConfig CRUD has moved out of the API surface — vector
store configuration is now in :class:`AppConfig`. The cascade test
that exercised the old CRUD route is gone; the registry-rebuild
behaviour is covered directly by
``tests/api/test_vector_store_registry.py``.
"""

from __future__ import annotations

import pytest

from matrix.model.collection import Collection, CollectionEmbedder, Document


_SSP_BODY = {
    "id": "ssp-test",
    "provider": "pgvector",
    "config": {
        "hostname": "localhost",
        "port": 5432,
        "database": "matrix",
        "username": "matrix",
        "password": "matrix",
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


class TestDocumentRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _document().model_dump(mode="json")
        post = await client.post("/v1/documents", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/documents/doc-1")
        assert get.status_code == 200
