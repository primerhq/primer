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
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport


async def _bootstrap_and_wait(
    client: httpx.AsyncClient, *, timeout_s: float = 30.0,
) -> dict:
    """POST /bootstrap and return the resulting status.

    S2: "bootstrap" is now "enable semantic search on the system
    collection", which runs inline, so there is nothing to poll. The
    signature keeps its name and timeout kwarg so call sites read the
    same.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code == 200, resp.text
    return resp.json()

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
# Module-level patch: suppress the real Docling/HuggingFace ingest path
# ===========================================================================

# _ingest_ai_docs() defaults to DoclingSplitter (HybridChunker backed by a
# sentence-transformers BertTokenizer) + DoclingLoader (IBM DocumentConverter).
# Both download ML models over :443 on a cold cache, causing the suite to hang
# in CI or on a pristine machine.  The API-surface tests in this file only care
# that the bootstrap lifecycle (status rows, subsystem attachment, collection
# creation) works correctly -- not that AI-doc markdown is actually embedded.
# We therefore stub _ingest_ai_docs to a coroutine that returns 0 immediately.
# The behaviour under a real ingester_factory is covered exhaustively by
# tests/test_internal_collections.py::TestAiDocsBootstrap.
@pytest.fixture(autouse=True)
def _stub_ingest_ai_docs():
    async def _noop(self, emit, counts, **kwargs):
        counts["docs"] = 0
        return 0

    with patch(
        "primer.internal_collections.InternalCollectionsSubsystem._ingest_ai_docs",
        new=_noop,
    ):
        yield


# ===========================================================================
# Local in-memory fakes
# ===========================================================================


def _predicate_equalities(predicate) -> list[tuple[str, Any]]:
    """Pull (field, value) equality / IS NULL pairs out of a built predicate.

    Q builds a BINARY tree of Predicate nodes: an AND node's left and right
    are themselves predicates, and only leaves carry a field. Treating the
    root as a leaf silently yields no filters, which makes find() return
    everything and every document look like a child of every parent.
    """
    out: list[tuple[str, Any]] = []

    def walk(node) -> None:
        if node is None:
            return
        op = getattr(node, "op", None)
        op_value = getattr(op, "value", op)
        if op_value == "and":
            walk(getattr(node, "left", None))
            walk(getattr(node, "right", None))
            return
        left = getattr(node, "left", None)
        name = getattr(left, "name", None)
        if name is None:
            return
        if op_value == "is_null":
            out.append((name, None))
            return
        if op_value == "=":
            right = getattr(node, "right", None)
            out.append((name, getattr(right, "value", None)))

    walk(predicate)
    return out


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str, *, conn=None):
        return self._data.get(id)

    async def create(self, e, *, conn=None):
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e, *, conn=None):
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id, *, conn=None):
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
        # The document tree filters by collection_id + parent_id; a fake
        # that ignores the predicate makes every node a child of every
        # other one, so honour the simple equality clauses.
        items = list(self._data.values())
        for field, value in _predicate_equalities(predicate):
            items = [
                i for i in items
                if (getattr(i, field, None) == value)
                or (value is None and getattr(i, field, None) is None)
            ]
        sliced = items[page.offset : page.offset + page.length]
        return OffsetPageResponse(
            offset=page.offset, length=len(sliced), total=len(items), items=sliced,
        )


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}
        # The document tree (and so the system-collection regeneration the
        # lifespan runs) needs a content store and a transaction context.
        from tests.conftest import _FakeContentStore, _NoOpTransaction

        self._content_store = _FakeContentStore()
        self._txn = _NoOpTransaction

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())

    def get_content_store(self):
        return self._content_store

    def transaction(self):
        return self._txn()

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
    # Regenerate the system collection (parity with the lifespan, which
    # does this unconditionally). This module builds its own app, so the
    # shared conftest mirror does not reach it.
    from primer.knowledge.system_collection import regenerate_system_collection
    await regenerate_system_collection(sp, toolset_providers={})
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
        model=AgentModel(profile_id="anthropic-1--claude-sonnet-4-6"),
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
        body2 = {**_config_body(), "cross_encoder": {
            "provider_id": "ce-1", "model": "rerank-m",
        }}
        resp = await client.put("/v1/internal_collections/config", json=body2)
        assert resp.status_code == 200, resp.text
        get = await client.get("/v1/internal_collections/config")
        assert get.json()["activated_at"] is not None
        assert get.json()["cross_encoder"]["model"] == "rerank-m"

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
        ext = resp.json()["extensions"]
        assert ext["error"] == "subsystem_active"
        assert "embedding_model" in ext["frozen_fields"]
        # Confirm the row was not mutated.
        get = await client.get("/v1/internal_collections/config")
        assert get.json()["embedding_model"] == _config_body()["embedding_model"]

    @pytest.mark.asyncio
    async def test_delete_clears_config_and_detaches_subsystem(
        self, client, app, store
    ) -> None:
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        assert app.state.internal_collections is not None
        # S2: activation enables search on the ONE system collection
        # rather than materialising five reserved _internal_* namespaces.
        coll = await client.get("/v1/collections/system")
        assert coll.json()["search"] is not None

        delete = await client.delete("/v1/internal_collections/config")
        assert delete.status_code == 204
        assert app.state.internal_collections is None
        search = await client.post(
            "/v1/agents/search", json={"query": "anything"}
        )
        assert search.status_code == 503

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
        enabled = await client.get("/v1/collections/system")
        assert enabled.json()["search"] is not None

        delete = await client.delete("/v1/internal_collections/config")
        assert delete.status_code == 204
        disabled = await client.get("/v1/collections/system")
        assert disabled.json()["search"] is None

        # Re-PUT + re-enable: the second activation starts from a
        # disabled collection, so a different embedding model cannot
        # surface dimension-mismatched stale vectors.
        put2 = await client.put(
            "/v1/internal_collections/config", json=_config_body()
        )
        assert put2.status_code == 200, put2.text
        await _bootstrap_and_wait(client)
        again = await client.get("/v1/collections/system")
        assert again.json()["search"] is not None


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

        # S2: enabling runs inline and reports the outcome directly.
        resp = await client.post("/v1/internal_collections/bootstrap")
        assert resp.status_code == 200, resp.text
        final = resp.json()
        assert final["status"] == "succeeded"
        assert final["state"] == "ready"
        assert final["collection_id"] == "system"
        assert final["search"]["vector_store_provider_id"] == "ssp-test"

    @pytest.mark.asyncio
    async def test_bootstrap_is_idempotent(self, client) -> None:
        """Enabling twice is a no-op re-index, not a conflict: there is no
        in-flight row to race on now that the work is inline."""
        await client.put("/v1/internal_collections/config", json=_config_body())
        first = await client.post("/v1/internal_collections/bootstrap")
        assert first.status_code == 200, first.text
        second = await client.post("/v1/internal_collections/bootstrap")
        assert second.status_code == 200, second.text
        assert second.json()["state"] == "ready"

    @pytest.mark.asyncio
    async def test_status_returns_idle_before_any_bootstrap(
        self, client,
    ) -> None:
        resp = await client.get("/v1/internal_collections/bootstrap/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "idle"
        assert body["state"] == "disabled"
        assert body["documents_indexed"] == 0

    @pytest.mark.asyncio
    async def test_status_reflects_terminal_state(
        self, client,
    ) -> None:
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        resp = await client.get("/v1/internal_collections/bootstrap/status")
        body = resp.json()
        assert body["status"] == "succeeded"
        assert body["state"] == "ready"
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
    async def test_search_resolves_but_is_inert_when_active(
        self, client, sp
    ) -> None:
        """S2 pinned decision 15: the per-entity IC search surface stays
        REGISTERED BUT INERT until P5 deletes it. Activation no longer
        populates the five _internal_* namespaces - agent and graph
        material lives in the system collection now, reachable through
        collections.semantic_search - so these routes resolve and answer
        with no hits rather than 503."""
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)

        resp = await client.post(
            "/v1/agents/search", json={"query": "research"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["hits"] == []


# ===========================================================================
# Search toolset (_search) integration through the registry
# ===========================================================================


class TestSearchToolset:
    @pytest.mark.asyncio
    async def test_search_toolset_id_no_longer_resolves(
        self, client, pr, sp
    ) -> None:
        """S2 P5 deleted the search toolset: collections.semantic_search is
        the live path, so the reserved id is simply unknown now."""
        from primer.model.except_ import NotFoundError

        await client.put("/v1/internal_collections/config", json=_config_body())
        await _bootstrap_and_wait(client)
        with pytest.raises(NotFoundError):
            await pr.get_toolset("search")

    @pytest.mark.asyncio
    async def test_search_toolset_unavailable_before_bootstrap(
        self, client, pr
    ) -> None:
        with pytest.raises(NotFoundError):
            await pr.get_toolset("search")


async def test_system_collection_exists_without_ic_activation(client):
    # M12: regeneration is unconditional; no embedder, no IC config needed.
    r = await client.get("/v1/collections/system")
    assert r.status_code == 200
    assert r.json()["system"] is True
    docs = await client.get("/v1/collections/system/docs",
                            params={"parent": "", "depth": 1})
    assert docs.status_code == 200
    roots = {n["slug"] for n in docs.json()["nodes"]}
    assert {"agents", "graphs", "tools", "collections", "docs"} <= roots


async def test_user_write_to_system_collection_403(client):
    r = await client.post("/v1/collections/system/docs",
                          json={"parent": "", "slug": "x", "body": "y"})
    assert r.status_code == 403
