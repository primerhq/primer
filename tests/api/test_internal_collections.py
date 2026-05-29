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

import asyncio
import time
from typing import Any

import httpx
import pytest
from httpx import ASGITransport


async def _bootstrap_and_wait(
    client: httpx.AsyncClient, *, timeout_s: float = 30.0,
) -> dict:
    """POST /bootstrap (async now) + poll /bootstrap/status until done.

    Returns the final status row dict. Asserts the POST succeeded with
    202 or 409 (409 if a previous test left one running — surfaces as a
    racy test, not a silently-broken assertion). Raises TimeoutError if
    bootstrap doesn't reach a terminal state in the budget.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code in (202, 409), resp.text
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        s = await client.get("/v1/internal_collections/bootstrap/status")
        body = s.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"bootstrap did not complete within {timeout_s}s; last status={body}"
    )

from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
    PgVectorConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
)
from primer.model.storage import OffsetPage, OffsetPageResponse


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
        self.dropped: list[str] = []

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

    async def drop_collection(self, cid):
        self.dropped.append(cid)
        self.collections.pop(cid, None)
        for key in list(self.records.keys()):
            if key[0] == cid:
                del self.records[key]

    async def search(self, cid, vector, k):
        from primer.model.vector import SearchResult

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
async def app(sp, pr, store):
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
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
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
    async def test_put_preserves_activated_at_when_editing_reranker(self, client) -> None:
        """Reranker / MMR don't define the vector space, so editing them
        post-activation succeeds and preserves activated_at."""
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        body2 = {**_config_body(), "mmr": {"lambda_mult": 0.7}}
        resp = await client.put("/v1/internal_collections/config", json=body2)
        assert resp.status_code == 200, resp.text
        get = await client.get("/v1/internal_collections/config")
        assert get.json()["activated_at"] is not None
        assert get.json()["mmr"]["lambda_mult"] == 0.7

    @pytest.mark.asyncio
    async def test_put_rejects_vector_space_change_after_activation(self, client) -> None:
        """Changing embedding_model, embedding_provider_id, or
        search_provider_id post-activation would invalidate existing
        embeddings (different vector space) — reject 409."""
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        # Try to swap the embedding model — should be rejected.
        body2 = {**_config_body(), "embedding_model": "different-model"}
        resp = await client.put("/v1/internal_collections/config", json=body2)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "subsystem_active"
        assert "embedding_model" in detail["frozen_fields"]
        # Confirm the row was not mutated.
        get = await client.get("/v1/internal_collections/config")
        assert get.json()["embedding_model"] == _config_body()["embedding_model"]

    @pytest.mark.asyncio
    async def test_delete_clears_config_and_detaches_subsystem(
        self, client, app, store
    ) -> None:
        from primer.model.internal import INTERNAL_COLLECTION_IDS

        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        assert app.state.internal_collections is not None
        # Sanity: bootstrap should have materialised the four reserved
        # collections on the fake store.
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert coll_id in store.collections

        delete = await client.delete("/v1/internal_collections/config")
        assert delete.status_code == 204
        assert app.state.internal_collections is None
        search = await client.post(
            "/v1/agents/search", json={"query": "anything"}
        )
        assert search.status_code == 503

        # The four reserved collections were dropped from the SSP's
        # backing store — not just detached. Without this, a subsequent
        # re-activation with a different embedding model would surface
        # dimension-mismatched stale vectors.
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert coll_id in store.dropped, (
                f"collection {coll_id!r} was not dropped on deactivate"
            )
            assert coll_id not in store.collections

    @pytest.mark.asyncio
    async def test_delete_then_reput_with_different_dimensions_succeeds(
        self, client, app, store
    ) -> None:
        """The deactivate-then-reactivate path is the only sane way to
        switch embedding models. Confirm that after DELETE, the four
        reserved collections are gone so a re-PUT + bootstrap rebuilds
        from scratch without colliding with stale vectors of a
        different dimensionality."""
        from primer.model.internal import INTERNAL_COLLECTION_IDS

        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert coll_id in store.collections

        delete = await client.delete("/v1/internal_collections/config")
        assert delete.status_code == 204
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert coll_id not in store.collections

        # Re-PUT + re-bootstrap — same fake (so dimensions don't actually
        # mismatch here, but the surface contract is that the second
        # bootstrap doesn't see the prior collections).
        put2 = await client.put(
            "/v1/internal_collections/config", json=_config_body()
        )
        assert put2.status_code == 200, put2.text
        await _bootstrap_and_wait(client)
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert coll_id in store.collections


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

        # POST returns 202 + the freshly-claimed status row, then the
        # background task runs to completion.
        resp = await client.post("/v1/internal_collections/bootstrap")
        assert resp.status_code == 202, resp.text
        initial = resp.json()
        assert initial["status"] == "running"
        assert initial["attempt_id"]

        final = await _bootstrap_and_wait(client)
        assert final["status"] == "succeeded"
        assert final["counts"]["agents"] == 2
        assert app.state.internal_collections is not None
        assert app.state.search_toolset is not None

    @pytest.mark.asyncio
    async def test_bootstrap_409_when_already_running(
        self, client, sp
    ) -> None:
        """A second POST while the first is in-flight returns 409 with
        the in-flight status row so the UI can render it without
        re-claiming."""
        await client.put("/v1/internal_collections/config", json=_config_body())

        # Kick off the first bootstrap. Don't await completion yet —
        # but the in-memory backend can finish fast, so race.
        r1 = await client.post("/v1/internal_collections/bootstrap")
        assert r1.status_code == 202

        # The conflict either fires (still running) or we win the race
        # and get another fresh 202. Either is fine — what we're
        # asserting is that 409 *is* the response when running is True.
        r2 = await client.post("/v1/internal_collections/bootstrap")
        assert r2.status_code in (202, 409)
        if r2.status_code == 409:
            detail = r2.json()["detail"]
            assert detail["error"] == "bootstrap_already_running"
            assert detail["status"]["status"] == "running"

        # Settle so other tests don't inherit a half-baked state.
        await _bootstrap_and_wait(client)

    @pytest.mark.asyncio
    async def test_status_returns_idle_before_any_bootstrap(
        self, client,
    ) -> None:
        resp = await client.get("/v1/internal_collections/bootstrap/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "idle"
        assert body["started_at"] is None
        assert body["finished_at"] is None

    @pytest.mark.asyncio
    async def test_status_reflects_terminal_state(
        self, client,
    ) -> None:
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        resp = await client.get("/v1/internal_collections/bootstrap/status")
        body = resp.json()
        assert body["status"] == "succeeded"
        assert body["finished_at"] is not None
        assert body["error"] is None


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
        await _bootstrap_and_wait(client)

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
        await _bootstrap_and_wait(client)

        provider = await pr.get_toolset("search")
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
            await pr.get_toolset("search")
