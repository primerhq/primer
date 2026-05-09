"""Phase-3 router tests: VectorStoreConfig + Collection + Document."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix.api.registries.vector_store_registry import (
    ACTIVE_VECTOR_STORE_CONFIG_ID,
)
from matrix.model.collection import Collection, CollectionEmbedder, Document
from matrix.model.vector import VectorStoreConfig


def _vsc(**overrides) -> VectorStoreConfig:
    body = dict(
        id=ACTIVE_VECTOR_STORE_CONFIG_ID,
        backend="pgvector",
        settings={"dsn": "x"},
    )
    body.update(overrides)
    return VectorStoreConfig(**body)


def _collection(**overrides) -> Collection:
    body = dict(
        id="kb-1",
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
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


class TestVectorStoreConfigRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _vsc().model_dump(mode="json")
        post = await client.post("/v1/vector_store_configs", json=body)
        assert post.status_code == 201, post.text
        get = await client.get(
            f"/v1/vector_store_configs/{ACTIVE_VECTOR_STORE_CONFIG_ID}"
        )
        assert get.status_code == 200

    @pytest.mark.asyncio
    async def test_create_invalidates_vector_store_registry(
        self, client, fake_vector_store_registry
    ) -> None:
        # Pre-cache a fake provider so we can observe the invalidation
        sentinel = MagicMock()
        sentinel.aclose = AsyncMock()
        fake_vector_store_registry._provider = sentinel  # type: ignore[attr-defined]
        fake_vector_store_registry._store = MagicMock()  # type: ignore[attr-defined]

        body = _vsc().model_dump(mode="json")
        resp = await client.post("/v1/vector_store_configs", json=body)
        assert resp.status_code == 201

        assert fake_vector_store_registry._provider is None  # type: ignore[attr-defined]
        sentinel.aclose.assert_awaited_once()


class TestCollectionRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _collection().model_dump(mode="json")
        post = await client.post("/v1/collections", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/collections/kb-1")
        assert get.status_code == 200

    @pytest.mark.asyncio
    async def test_list_collection_documents_works_when_collection_exists(
        self, client
    ) -> None:
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
