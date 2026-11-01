"""Collection queries are embedded as queries (KN-6, ruling D3).

Every query door (the REST route, the collections ``search`` tool, ``system__search_collection``, the plain path of
``run_collection_search`` and :class:`CollectionSearcher`) asks the embedder for ``task_type="retrieval_query"``, so BGE gets its
query instruction and E5 / nomic get ``query:`` / ``search_query:`` instead of the document prefix. Documents are untouched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from primer.embedder._prompts import select_prompt
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.embedding import EmbedResponse, Embedding
from primer.model.provider import SqliteConfig
from primer.model.search import CollectionCrossEncoder
from primer.search.run import run_collection_search
from primer.search.searcher import CollectionSearcher
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.caller import AsCaller


class _RecEmb:
    """Records the ``config`` of every embed call."""

    def __init__(self) -> None:
        self.configs: list = []

    async def embed(self, *, model, inputs, output_dimensions=None, config=None):
        self.configs.append(config)
        return EmbedResponse(model=model, embeddings=[Embedding(index=i, vector=[0.1, 0.2]) for i in range(len(inputs))])


class _EmptyStore:
    async def search(self, cid, vector, k):
        return []


class _CE:
    async def score(self, *, model, query, documents, batch_size=32):
        return [0.0] * len(documents)


class _Resolver:
    async def get_cross_encoder(self, provider_id):
        return _CE()


def _coll(*, cross_encoder: CollectionCrossEncoder | None = None) -> Collection:
    return Collection(id="kb", description="kb", search=CollectionSearchConfig(
        embedder=CollectionEmbedder(provider_id="emb", model="BAAI/bge-small-en-v1.5"),
        vector_store_provider_id="ssp", state="ready", cross_encoder=cross_encoder,
    ))


def _is_query(configs) -> bool:
    return bool(configs) and all(c is not None and c.task_type == "retrieval_query" for c in configs)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_storage(Collection).create(_coll())
    yield provider
    await provider.aclose()


# ---- characterization: unchanged by D3 ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["sentence-transformers/all-MiniLM-L6-v2", "thenlper/gte-small", "text-embedding-3-small"])
def test_a_model_with_no_query_prompt_embeds_a_query_as_before(model):
    assert select_prompt(task_type="retrieval_query", model_name=model) == select_prompt(task_type=None, model_name=model) is None


async def test_documents_are_still_embedded_as_documents():
    from primer.knowledge.indexing import index_document

    class _Content:
        async def get(self, document_id):
            return "hello world"

    class _Store:
        async def create_collection(self, cid, *, dimensions, distance="cosine"):
            pass

        async def delete(self, cid, did):
            pass

        async def put(self, r):
            pass

        async def put_many(self, rs):
            pass

    emb = _RecEmb()
    reg, ssr = AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_Store())
    doc = Document(id="d1", collection_id="kb", slug="a", path="a", meta={})

    await index_document(document=doc, collection=_coll(), provider_registry=reg, semantic_search_registry=ssr,
                         content_store=_Content(), dimensions=2)

    assert emb.configs and all(c is None or c.task_type != "retrieval_query" for c in emb.configs)


# ---- red: every query door asks for the query embedding -------------------------------------------------------------------------------


async def test_collection_searcher_embeds_the_query_as_a_query():
    emb = _RecEmb()
    await CollectionSearcher(collection=_coll(), embedder=emb, vector_store=_EmptyStore()).search("web search", 3)
    assert _is_query(emb.configs)


async def test_run_collection_search_plain_path_embeds_as_a_query():
    emb = _RecEmb()
    await run_collection_search(collection=_coll(), embedder=emb, store=_EmptyStore(), query="q", top_k=3,
                                cross_encoder_resolver=None)
    assert _is_query(emb.configs)


async def test_run_collection_search_rerank_path_embeds_as_a_query():
    emb = _RecEmb()
    await run_collection_search(collection=_coll(cross_encoder=CollectionCrossEncoder(provider_id="ce", model="x")),
                                embedder=emb, store=_EmptyStore(), query="q", top_k=3, cross_encoder_resolver=_Resolver())
    assert _is_query(emb.configs)


async def test_rest_search_route_embeds_as_a_query():
    from primer.api.routers.knowledge import _CollectionSearchBody, search_collection

    emb = _RecEmb()
    collections = AsyncMock()
    collections.get = AsyncMock(return_value=_coll())
    registry, ssr = AsyncMock(), AsyncMock()
    registry.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_EmptyStore())
    admin = SimpleNamespace(state=SimpleNamespace(actor=SimpleNamespace(type="user", user_id="admin", role="admin")))

    out = await search_collection(request=admin, collection_id="kb", body=_CollectionSearchBody(query="q", top_k=3),
                                  collections=collections, registry=registry, ssr=ssr)

    assert out == {"hits": []}
    assert _is_query(emb.configs)


async def test_collections_search_tool_embeds_as_a_query(sp):
    from primer.toolset.collections import build_collections_toolset

    emb = _RecEmb()
    reg, ssr = AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_EmptyStore())
    tools = AsCaller(build_collections_toolset(storage_provider=sp, provider_registry=reg, semantic_search_registry=ssr))

    res = await tools.call(tool_name="search", arguments={"collection": "kb", "query": "q", "mode": "semantic"})

    assert not res.is_error, res.output
    assert json.loads(res.output)["mode_used"] == "semantic"
    assert _is_query(emb.configs)


async def test_system_search_collection_tool_embeds_as_a_query(sp):
    from primer.toolset.system import build_system_toolset

    emb = _RecEmb()
    reg, ssr = AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_EmptyStore())
    tools = AsCaller(build_system_toolset(storage_provider=sp, provider_registry=reg, semantic_search_registry=ssr))

    res = await tools.call(tool_name="search_collection", arguments={"collection_id": "kb", "query": "q"})

    assert not res.is_error, res.output
    assert _is_query(emb.configs)
