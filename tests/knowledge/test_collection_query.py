"""``run_collection_query``: the search ladder of the collections ``search`` tool, callable without a tool context (agent memory T11a)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.knowledge.collection_query import QueryHit, run_collection_query
from primer.model.chat import TextPart
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.embedding import EmbedResponse, Embedding
from primer.model.except_ import ConflictError
from primer.model.provider import SqliteConfig
from primer.model.vector import EmbeddingRecord, SearchResult
from primer.storage.sqlite import SqliteStorageProvider


class _Emb:
    async def embed(self, *, model, inputs, output_dimensions=None, config=None):
        return EmbedResponse(model=model, embeddings=[Embedding(index=i, vector=[0.1, 0.2]) for i in range(len(inputs))])


class _Store:
    """Answers every query with the same ranked chunks, best first; ``k`` is honoured."""

    def __init__(self, hits):
        self.hits = hits
        self.asked_k: list[int] = []

    async def search(self, collection_id, vector, k):
        self.asked_k.append(k)
        return self.hits[:k]


class _Registry:
    def __init__(self, store):
        self.store = store

    async def get_embedder(self, provider_id):
        return _Emb()

    async def get_store(self, provider_id):
        return self.store


def _hit(path: str, score: float, text: str = "chunk") -> SearchResult:
    record = EmbeddingRecord(collection_id="kb", document_id=f"doc-{path}", chunk_id="c0", text=text, vector=[0.1, 0.2], meta={"path": path})
    return SearchResult(record=record, score=score)


SEMANTIC = CollectionSearchConfig(
    embedder=CollectionEmbedder(provider_id="emb", model="m"), vector_store_provider_id="ssp", state="ready",
)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    content = provider.get_content_store()
    for i, (path, body) in enumerate([
        ("active/retry-429", "When the API answers 429, wait for the retry-after header."),
        ("active/pagination", "Page through results with the cursor until it is empty."),
        ("candidates/cache", "Cache the token between calls."),
    ]):
        await content.upsert(document_id=f"d{i}", collection_id="kb", path=path, content=body)
    yield provider
    await provider.aclose()


def _coll(search=None) -> Collection:
    return Collection(id="kb", description="kb", search=search)


async def test_semantic_hits_carry_scores_and_the_mode(sp):
    store = _Store([_hit("active/retry-429", 0.9, "retry text"), _hit("active/pagination", 0.5)])
    registry = _Registry(store)
    hits, mode = await run_collection_query(
        sp=sp, provider_registry=registry, semantic_search_registry=registry, collection=_coll(SEMANTIC), query="rate limit", limit=5,
    )
    assert mode == "semantic"
    assert hits == [QueryHit("active/retry-429", "retry text", 0.9), QueryHit("active/pagination", "chunk", 0.5)]


async def test_path_prefix_filters_semantic_hits_and_limit_caps_them(sp):
    store = _Store([_hit("candidates/cache", 0.95), _hit("active/retry-429", 0.9), _hit("active/pagination", 0.8)])
    registry = _Registry(store)
    common = dict(sp=sp, provider_registry=registry, semantic_search_registry=registry, collection=_coll(SEMANTIC), query="q")
    hits, _ = await run_collection_query(**common, limit=5, path_prefix="active/")
    assert [h.path for h in hits] == ["active/retry-429", "active/pagination"]
    hits, _ = await run_collection_query(**common, limit=1, path_prefix="/active")
    assert [h.path for h in hits] == ["active/retry-429"], "a leading slash is ignored; the limit applies after the filter"
    assert max(store.asked_k) >= 100, "a prefix asks the store for more than the limit, as the tool does"


async def test_full_text_fallback_has_no_comparable_score(sp):
    hits, mode = await run_collection_query(
        sp=sp, provider_registry=None, semantic_search_registry=None, collection=_coll(), query="retry-after header", limit=5,
    )
    assert mode == "fulltext"
    assert [h.path for h in hits] == ["active/retry-429"] and hits[0].score is None and "429" in hits[0].excerpt


async def test_full_text_honours_path_prefix_and_limit(sp):
    common = dict(sp=sp, provider_registry=None, semantic_search_registry=None, collection=_coll(), query="the")
    hits, _ = await run_collection_query(**common, limit=5, path_prefix="candidates/")
    assert [h.path for h in hits] == ["candidates/cache"]
    hits, _ = await run_collection_query(**common, limit=1)
    assert len(hits) == 1


async def test_a_collection_whose_index_is_not_ready_falls_back_to_full_text(sp):
    building = SEMANTIC.model_copy(update={"state": "indexing"})
    registry = _Registry(_Store([_hit("active/retry-429", 0.9)]))
    hits, mode = await run_collection_query(
        sp=sp, provider_registry=registry, semantic_search_registry=registry, collection=_coll(building), query="cursor", limit=5,
    )
    assert mode == "fulltext" and [h.path for h in hits] == ["active/pagination"]


async def test_literal_and_regex_modes(sp):
    common = dict(sp=sp, provider_registry=None, semantic_search_registry=None, collection=_coll(), limit=5)
    hits, mode = await run_collection_query(**common, query="Cache the token", mode="literal")
    assert mode == "literal" and [h.path for h in hits] == ["candidates/cache"]
    hits, mode = await run_collection_query(**common, query=r"cursor\s+until", mode="regex")
    assert mode == "regex" and [h.path for h in hits] == ["active/pagination"]


async def test_asking_for_semantic_where_there_is_none_is_a_conflict(sp):
    with pytest.raises(ConflictError, match="semantic search is not enabled on 'kb'"):
        await run_collection_query(sp=sp, provider_registry=None, semantic_search_registry=None, collection=_coll(), query="q", limit=3, mode="semantic")


async def test_an_unusable_provider_holds_semantic_back(sp):
    """The person's own providers: a semantic collection whose embedder the person may not use answers from full text."""
    from primer.access.person import Person
    from primer.model.access import Access
    from primer.model.provider import EmbeddingProvider

    await sp.get_storage(EmbeddingProvider).create(EmbeddingProvider(
        id="emb", provider="openai", models=[{"name": "m"}], config={"url": "http://e.internal/v1", "api_key": "k", "flavor": "other"},
        limits={"max_concurrency": 1}, access=Access(owners=["someone-else"]),
    ))
    registry = _Registry(_Store([_hit("active/retry-429", 0.9)]))
    common = dict(sp=sp, provider_registry=registry, semantic_search_registry=registry, collection=_coll(SEMANTIC), query="cursor", limit=3)
    hits, mode = await run_collection_query(**common, person=Person("u1", "user"))
    assert mode == "fulltext"
    with pytest.raises(ConflictError, match="not available to you"):
        await run_collection_query(**common, person=Person("u1", "user"), mode="semantic")
    _hits, mode = await run_collection_query(**common, person=Person("someone-else", "user"))
    assert mode == "semantic"
