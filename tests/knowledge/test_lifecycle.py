"""enable_search backfills, disable_search drops the namespace."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest_asyncio

from primer.knowledge.lifecycle import disable_search, enable_search, search_status
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import (
    Collection, CollectionEmbedder, CollectionSearchConfig,
)
from primer.model.provider import (
    EmbeddingModel, EmbeddingProvider, EmbeddingProviderType, HuggingFaceConfig,
    Limits, SemanticSearchProvider, SqliteConfig, StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from tests.knowledge.test_indexing import _Emb, _Store

from pydantic import SecretStr


@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()

    await provider.get_storage(EmbeddingProvider).create(EmbeddingProvider(
        id="emb-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="m")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=1),
    ))
    await provider.get_storage(SemanticSearchProvider).create(
        SemanticSearchProvider.model_validate({
            "id": "ssp-1",
            "provider": "pgvector",
            "config": {
                "hostname": "localhost",
                "port": 5432,
                "database": "primer",
                "username": "primer",
                "password": "primer",
                "db_schema": "public",
            },
        })
    )
    await provider.get_storage(Collection).create(
        Collection(id="c1", description="wiki")
    )
    tree = DocumentTreeService(provider)
    for slug in ("a", "b", "c"):
        await tree.create(collection_id="c1", parent="", slug=slug, body=f"body {slug}")

    store = _Store()
    reg = AsyncMock()
    reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    ssr = AsyncMock()
    ssr.get_store = AsyncMock(return_value=store)
    yield provider, reg, ssr, store
    await provider.aclose()


def _cfg() -> CollectionSearchConfig:
    return CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="emb-1", model="m"),
        vector_store_provider_id="ssp-1",
    )


async def test_enable_backfills_every_document(env):
    provider, reg, ssr, store = env
    updated = await enable_search(
        provider, reg, ssr, collection_id="c1", cfg=_cfg(),
    )
    assert updated.search is not None
    assert updated.search.state == "ready"
    assert len({r.document_id for r in store.puts}) == 3


async def test_status_reports_totals(env):
    provider, reg, ssr, store = env
    await enable_search(provider, reg, ssr, collection_id="c1", cfg=_cfg())
    ssr.get_store = AsyncMock(return_value=store)
    status = await search_status(provider, ssr, collection_id="c1")
    assert status.state == "ready"
    assert status.documents_total == 3


async def test_disable_drops_the_namespace(env):
    provider, reg, ssr, store = env
    await enable_search(provider, reg, ssr, collection_id="c1", cfg=_cfg())
    updated = await disable_search(provider, ssr, collection_id="c1")
    assert updated.search is None
