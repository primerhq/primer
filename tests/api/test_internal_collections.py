"""End-to-end tests for the internal collections REST surface.

Covers:

* Config CRUD (PUT/GET/DELETE).
* Bootstrap endpoint — builds the live subsystem on the running app
  when none was attached at boot, then ingests entities + tools.
* Per-entity ``/search`` endpoints — 503 when inactive, hits when
  active.
* Cascade: ``DELETE /v1/internal_collections/config`` detaches the
  live subsystem.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from pydantic import SecretStr

from matrix.api.app import create_test_app
from matrix.api.registries import ProviderRegistry, VectorStoreRegistry
from matrix.model.agent import Agent, AgentModel
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
    PgVectorConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
    VectorStoreProviderConfig,
    VectorStoreProviderType,
)
from matrix.model.storage import OffsetPage, OffsetPageResponse


# ===========================================================================
# Local in-memory fakes
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, e):
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e):
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id):
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            return OffsetPageResponse(
                offset=page.offset,
                length=len(items[page.offset : page.offset + page.length]),
                total=len(items),
                items=items[page.offset : page.offset + page.length],
            )
        return OffsetPageResponse(
            offset=0, length=len(items), total=len(items), items=items
        )

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self):
        return

    async def aclose(self):
        return


class _FakeStore:
    def __init__(self) -> None:
        self.collections: dict = {}
        self.records: dict = {}

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.collections[cid] = {"dimensions": dimensions, "distance": distance}

    async def put(self, record):
        self.records[(record.collection_id, record.document_id, record.chunk_id)] = (
            record
        )

    async def delete(self, cid, doc_id):
        for key in list(self.records.keys()):
            if key[0] == cid and key[1] == doc_id:
                del self.records[key]

    async def search(self, cid, vector, k):
        from matrix.model.vector import SearchResult

        return [
            SearchResult(record=r, score=1.0)
            for (c, _, _), r in self.records.items()
            if c == cid
        ][:k]


class _FakeEmbedder:
    async def embed(self, *, model, inputs, **kwargs):
        class _R:
            embeddings = [type("E", (), {"vector": [0.1, 0.2, 0.3, 0.4]})()]

        return _R()


class _FakeSSR:
    """Fake SemanticSearchRegistry: returns the given store for any ssp_id."""

    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    async def get_store(self, ssp_id: str) -> _FakeStore:
        return self._store

    async def aclose(self) -> None:
        return


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture
def embedder() -> _FakeEmbedder:
    return _FakeEmbedder()


@pytest.fixture
def pr(sp, embedder) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: embedder,
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


class _Provider:
    def __init__(self, store):
        self._store = store

    async def initialize(self):
        return

    async def aclose(self):
        return

    def get_vector_store(self):
        return self._store


@pytest.fixture
def vsr(store) -> VectorStoreRegistry:
    cfg = VectorStoreProviderConfig(
        provider=VectorStoreProviderType.PGVECTOR,
        config=PgVectorConfig(
            hostname="x",
            username="u",
            password="p",  # type: ignore[arg-type]
            database="d",
        ),
    )
    return VectorStoreRegistry(cfg, factory=lambda c: _Provider(store))


@pytest.fixture
async def app(sp, pr, vsr, store):
    # Seed the embedding provider row that the subsystem will look up
    # via ProviderRegistry.get_embedder. Tests that exercise bootstrap
    # rely on this row being present before they POST.
    await sp.get_storage(EmbeddingProvider).create(
        EmbeddingProvider(
            id="hf-1",
            provider=EmbeddingProviderType.HUGGINGFACE,
            models=[EmbeddingModel(name="all-MiniLM-L6-v2")],
            config=HuggingFaceConfig(token=SecretStr("x")),
            limits=Limits(max_concurrency=2),
        )
    )
    # Seed the SSP row that put_config validates against.
    await sp.get_storage(SemanticSearchProvider).create(
        SemanticSearchProvider(
            id="ssp-test",
            provider=SemanticSearchProviderType.PGVECTOR,
            config=PgVectorConfig(
                hostname="x", username="u", password="p", database="d",  # type: ignore[arg-type]
            ),
        )
    )
    test_app = create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        vector_store_registry=vsr,
    )
    # Override the semantic_search_registry with a fake that returns the
    # test store so bootstrap can resolve vectors without a real database.
    test_app.state.semantic_search_registry = _FakeSSR(store)
    return test_app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _agent(id="agt-1") -> Agent:
    return Agent(
        id=id,
        description="research agent",
        model=AgentModel(provider_id="anthropic-1", model_name="claude-sonnet-4-6"),
        tools=[],
        system_prompt=["find papers"],
    )


def _config_body() -> dict:
    return {
        "embedding_provider_id": "hf-1",
        "embedding_model": "all-MiniLM-L6-v2",
        "search_provider_id": "ssp-test",
    }


# ===========================================================================
# Config CRUD
# ===========================================================================


class TestConfigCRUD:
    @pytest.mark.asyncio
    async def test_get_404_when_unconfigured(self, client) -> None:
        resp = await client.get("/v1/internal_collections/config")
        assert resp.status_code == 404
        assert resp.json()["type"] == "/errors/not-found"

    @pytest.mark.asyncio
    async def test_put_creates_and_get_returns(self, client) -> None:
        put = await client.put(
            "/v1/internal_collections/config", json=_config_body()
        )
        assert put.status_code == 200, put.text
        assert put.json()["embedding_provider_id"] == "hf-1"

        get = await client.get("/v1/internal_collections/config")
        assert get.status_code == 200
        assert get.json()["activated_at"] is None

    @pytest.mark.asyncio
    async def test_put_preserves_activated_at_on_update(self, client) -> None:
        await client.put("/v1/internal_collections/config", json=_config_body())
        await client.post("/v1/internal_collections/bootstrap")
        body2 = {**_config_body(), "embedding_model": "different-model"}
        await client.put("/v1/internal_collections/config", json=body2)
        get = await client.get("/v1/internal_collections/config")
        assert get.json()["activated_at"] is not None

    @pytest.mark.asyncio
    async def test_delete_clears_config_and_detaches_subsystem(
        self, client, app
    ) -> None:
        await client.put("/v1/internal_collections/config", json=_config_body())
        await client.post("/v1/internal_collections/bootstrap")
        assert app.state.internal_collections is not None

        delete = await client.delete("/v1/internal_collections/config")
        assert delete.status_code == 204
        assert app.state.internal_collections is None
        search = await client.post(
            "/v1/agents/search", json={"query": "anything"}
        )
        assert search.status_code == 503


# ===========================================================================
# Bootstrap
# ===========================================================================


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_404_when_no_config(self, client) -> None:
        resp = await client.post("/v1/internal_collections/bootstrap")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_bootstrap_builds_subsystem_and_returns_counts(
        self, client, app, sp
    ) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await sp.get_storage(Agent).create(_agent("agt-2"))
        await client.put("/v1/internal_collections/config", json=_config_body())

        resp = await client.post("/v1/internal_collections/bootstrap")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["counts"]["agents"] == 2
        assert app.state.internal_collections is not None
        assert app.state.search_toolset is not None


# ===========================================================================
# Per-entity search
# ===========================================================================


class TestSearchEndpoints:
    @pytest.mark.asyncio
    async def test_search_returns_503_when_inactive(self, client) -> None:
        for entity in ("agents", "graphs", "collections", "tools"):
            resp = await client.post(
                f"/v1/{entity}/search", json={"query": "anything"}
            )
            assert resp.status_code == 503
            assert resp.json()["type"] == "/errors/subsystem-inactive"

    @pytest.mark.asyncio
    async def test_search_returns_hits_when_active(self, client, sp) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await client.put("/v1/internal_collections/config", json=_config_body())
        await client.post("/v1/internal_collections/bootstrap")

        resp = await client.post(
            "/v1/agents/search", json={"query": "research"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = {h["document_id"] for h in body["hits"]}
        assert "agt-1" in ids


# ===========================================================================
# Search toolset (_search) integration through the registry
# ===========================================================================


class TestSearchToolset:
    @pytest.mark.asyncio
    async def test_search_toolset_resolves_after_activation(
        self, client, app, pr, sp
    ) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await client.put("/v1/internal_collections/config", json=_config_body())
        await client.post("/v1/internal_collections/bootstrap")

        provider = await pr.get_toolset("_search")
        names = [t.id async for t in provider.list_tools()]
        assert "search_agents" in names
        assert "search_graphs" in names
        assert "search_collections" in names
        assert "search_tools" in names

        result = await provider.call(
            tool_name="search_agents",
            arguments={"query": "research", "top_k": 5},
        )
        assert not result.is_error, result.output
        import json

        body = json.loads(result.output)
        ids = [h["document_id"] for h in body["hits"]]
        assert "agt-1" in ids

    @pytest.mark.asyncio
    async def test_search_toolset_unavailable_before_bootstrap(
        self, client, pr
    ) -> None:
        with pytest.raises(NotFoundError):
            await pr.get_toolset("_search")
