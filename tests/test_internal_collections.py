"""Unit tests for the internal collections subsystem core.

Covers the bootstrap orchestrator, the CDC event worker, the
ingest-failure log, and the search dispatch — all against in-memory
fakes for storage / embedder / vector store. The end-to-end API +
toolset wiring is tested separately in ``tests/api`` and
``tests/toolset``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from matrix.internal_collections import (
    INTERNAL_COLLECTION_IDS,
    IngestEvent,
    InternalCollectionsSubsystem,
    build_subsystem,
    embedding_text_for,
)
from matrix.model.agent import Agent, AgentModel
from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.except_ import ConfigError, ConflictError, NotFoundError
from matrix.model.internal import (
    INTERNAL_COLLECTIONS_CONFIG_ID,
    IngestFailure,
    InternalCollectionsConfig,
)
from matrix.model.storage import OffsetPage, OffsetPageResponse


# ===========================================================================
# In-memory fakes
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str) -> Any | None:
        return self._data.get(id)

    async def create(self, e: Any) -> Any:
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e: Any) -> Any:
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(
            offset=0, length=len(items), total=len(items), items=items
        )

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())


class _FakeVectorStore:
    """In-memory vector store stub: tracks every put / delete / search."""

    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.records: dict[tuple[str, str, str], Any] = {}
        self.deletes: list[tuple[str, str]] = []
        self.searches: list[tuple[str, list[float], int]] = []

    async def create_collection(
        self, collection_id, *, dimensions, distance="cosine"
    ):
        self.collections[collection_id] = {
            "dimensions": dimensions,
            "distance": distance,
        }

    async def put(self, record):
        key = (record.collection_id, record.document_id, record.chunk_id)
        self.records[key] = record

    async def delete(self, collection_id, document_id):
        self.deletes.append((collection_id, document_id))
        for key in list(self.records.keys()):
            if key[0] == collection_id and key[1] == document_id:
                del self.records[key]

    async def search(self, collection_id, vector, k):
        self.searches.append((collection_id, list(vector), k))
        from matrix.model.vector import SearchResult

        hits = []
        for (cid, _, _), record in self.records.items():
            if cid != collection_id:
                continue
            hits.append(SearchResult(record=record, score=1.0))
        return hits[:k]


class _FakeVSR:
    def __init__(self, store: _FakeVectorStore) -> None:
        self._store = store
        self.is_configured = True

    async def get(self):
        return self._store

    async def aclose(self) -> None:
        return


class _FakeEmbedder:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[tuple[str, list[Any]]] = []

    async def embed(self, *, model, inputs, **kwargs):
        self.calls.append((model, list(inputs)))

        class _R:
            def __init__(self, vec):
                self.embeddings = [type("E", (), {"vector": vec})()]

        text = inputs[0].text
        vec = [float((hash(text) >> i) & 0xFF) / 255.0 for i in range(self.dim)]
        return _R(vec)


class _FakePR:
    def __init__(self, embedder: _FakeEmbedder) -> None:
        self._embedder = embedder
        self.toolsets: dict[str, Any] = {}

    async def get_embedder(self, _provider_id):
        return self._embedder

    async def get_toolset(self, toolset_id):
        return self.toolsets[toolset_id]


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def cfg() -> InternalCollectionsConfig:
    return InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID,
        embedding_provider_id="hf-1",
        embedding_model="all-MiniLM-L6-v2",
        cross_encoder=None,
        mmr=None,
        activated_at=None,
    )


@pytest.fixture
def store() -> _FakeVectorStore:
    return _FakeVectorStore()


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def embedder() -> _FakeEmbedder:
    return _FakeEmbedder()


@pytest.fixture
def pr(embedder) -> _FakePR:
    return _FakePR(embedder)


@pytest.fixture
def vsr(store) -> _FakeVSR:
    return _FakeVSR(store)


@pytest.fixture
def subsystem(cfg, sp, pr, vsr) -> InternalCollectionsSubsystem:
    return build_subsystem(
        config=cfg,
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,  # type: ignore[arg-type]
        vector_store_registry=vsr,  # type: ignore[arg-type]
    )


def _agent(id="agt-1") -> Agent:
    return Agent(
        id=id,
        description="research agent that finds papers",
        model=AgentModel(provider_id="anthropic-1", model_name="claude-sonnet-4-6"),
        tools=[],
        system_prompt=["you find scientific papers"],
    )


def _collection(id="kb-1") -> Collection:
    return Collection(
        id=id,
        description="knowledge base of articles",
        embedder=CollectionEmbedder(provider_id="hf-1", model="m"),
        search_provider_id="ssp-test",
    )


# ===========================================================================
# Embedding-text extraction
# ===========================================================================


class TestEmbeddingTextExtraction:
    def test_agent_uses_description_plus_system_prompt(self) -> None:
        text = embedding_text_for(
            "agent",
            {
                "id": "a",
                "description": "researcher",
                "system_prompt": ["find papers", "cite sources"],
            },
        )
        assert "researcher" in text
        assert "find papers" in text

    def test_collection_uses_description(self) -> None:
        text = embedding_text_for(
            "collection",
            {"id": "kb-1", "description": "the docs"},
        )
        assert text == "the docs"

    def test_tool_uses_id_and_description(self) -> None:
        text = embedding_text_for(
            "tool",
            {"id": "search_agents", "description": "find agents by query"},
        )
        assert "search_agents" in text
        assert "find agents by query" in text

    def test_falls_back_to_id_when_no_description(self) -> None:
        text = embedding_text_for("agent", {"id": "fallback-id"})
        assert text == "fallback-id"


# ===========================================================================
# Bootstrap
# ===========================================================================


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_creates_collections_and_marks_activated(
        self, subsystem, store, sp
    ) -> None:
        result = await subsystem.bootstrap()
        assert result["ok"] is True
        coll_storage = sp.get_storage(Collection)
        for coll_id in INTERNAL_COLLECTION_IDS.values():
            assert await coll_storage.get(coll_id) is not None
            assert coll_id in store.collections
        assert subsystem.is_activated is True
        cfg_storage = sp.get_storage(InternalCollectionsConfig)
        persisted = await cfg_storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
        assert persisted is not None
        assert persisted.activated_at is not None
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_bootstrap_ingests_existing_agents(
        self, subsystem, store, sp
    ) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await sp.get_storage(Agent).create(_agent("agt-2"))
        result = await subsystem.bootstrap()
        assert result["counts"]["agents"] == 2
        agents_coll = INTERNAL_COLLECTION_IDS["agent"]
        assert (agents_coll, "agt-1", "0") in store.records
        assert (agents_coll, "agt-2", "0") in store.records
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_bootstrap_ingests_collections(
        self, subsystem, store, sp
    ) -> None:
        await sp.get_storage(Collection).create(_collection("kb-1"))
        result = await subsystem.bootstrap()
        assert result["counts"]["collections"] >= 1
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_bootstrap_is_idempotent(self, subsystem, store, sp) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        first = await subsystem.bootstrap()
        second = await subsystem.bootstrap()
        assert first["counts"]["agents"] == 1
        assert second["counts"]["agents"] == 1
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_bootstrap_ingests_injected_toolset_providers(
        self, cfg, sp, pr, vsr, store
    ) -> None:
        from matrix.model.chat import Tool

        class _ToolsetProvider:
            async def list_tools(self, principal=None):
                for tn in ("foo", "bar"):
                    yield Tool(
                        id=tn,
                        toolset_id="_system",
                        description=f"{tn} description",
                        args_schema={"type": "object"},
                    )

        subsystem = build_subsystem(
            config=cfg,
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=pr,  # type: ignore[arg-type]
            vector_store_registry=vsr,  # type: ignore[arg-type]
            toolset_providers={"_system": _ToolsetProvider()},
        )
        result = await subsystem.bootstrap()
        assert result["counts"]["tools"] == 2
        tools_coll = INTERNAL_COLLECTION_IDS["tool"]
        assert (tools_coll, "_system::foo", "0") in store.records
        assert (tools_coll, "_system::bar", "0") in store.records
        await subsystem.aclose()


# ===========================================================================
# CDC worker
# ===========================================================================


class TestCDCWorker:
    @pytest.mark.asyncio
    async def test_enqueue_then_worker_applies_upsert(
        self, subsystem, store
    ) -> None:
        await subsystem.bootstrap()
        subsystem.start_worker()
        subsystem.enqueue(
            IngestEvent(
                op="upsert",
                entity_type="agent",
                entity_id="agt-99",
                payload=_agent("agt-99").model_dump(mode="json"),
            )
        )
        for _ in range(50):
            if (
                INTERNAL_COLLECTION_IDS["agent"],
                "agt-99",
                "0",
            ) in store.records:
                break
            await asyncio.sleep(0.02)
        assert (
            INTERNAL_COLLECTION_IDS["agent"],
            "agt-99",
            "0",
        ) in store.records
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_enqueue_then_worker_applies_delete(
        self, subsystem, store
    ) -> None:
        await subsystem.bootstrap()
        subsystem.start_worker()
        subsystem.enqueue(
            IngestEvent(
                op="upsert",
                entity_type="agent",
                entity_id="agt-x",
                payload=_agent("agt-x").model_dump(mode="json"),
            )
        )
        await asyncio.sleep(0.05)
        subsystem.enqueue(
            IngestEvent(op="delete", entity_type="agent", entity_id="agt-x")
        )
        for _ in range(50):
            if (
                INTERNAL_COLLECTION_IDS["agent"],
                "agt-x",
            ) in store.deletes:
                break
            await asyncio.sleep(0.02)
        assert (INTERNAL_COLLECTION_IDS["agent"], "agt-x") in store.deletes
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_failed_event_logs_to_ingest_failure_table(
        self, subsystem, sp, embedder
    ) -> None:
        await subsystem.bootstrap()
        subsystem.start_worker()

        async def _boom(**kwargs):
            raise RuntimeError("embedder unavailable")

        embedder.embed = _boom  # type: ignore[assignment]

        subsystem.enqueue(
            IngestEvent(
                op="upsert",
                entity_type="agent",
                entity_id="agt-broken",
                payload=_agent("agt-broken").model_dump(mode="json"),
            )
        )
        for _ in range(50):
            failures = list(sp.get_storage(IngestFailure)._data.values())
            if any(f.entity_id == "agt-broken" for f in failures):
                break
            await asyncio.sleep(0.02)
        failures = list(sp.get_storage(IngestFailure)._data.values())
        relevant = [f for f in failures if f.entity_id == "agt-broken"]
        assert relevant, "expected an IngestFailure row for agt-broken"
        assert relevant[0].op == "upsert"
        assert "embedder unavailable" in relevant[0].error
        await subsystem.aclose()


# ===========================================================================
# Search
# ===========================================================================


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_raises_when_inactive(self, subsystem) -> None:
        with pytest.raises(ConfigError, match="not been bootstrapped"):
            await subsystem.search("agent", query="anything", top_k=5)

    @pytest.mark.asyncio
    async def test_search_returns_hits_after_bootstrap(
        self, subsystem, sp, store
    ) -> None:
        await sp.get_storage(Agent).create(_agent("agt-1"))
        await subsystem.bootstrap()
        hits = await subsystem.search("agent", query="paper finder", top_k=5)
        ids = [h.record.document_id for h in hits]
        assert "agt-1" in ids
        coll_ids = [c[0] for c in store.searches]
        assert INTERNAL_COLLECTION_IDS["agent"] in coll_ids
        await subsystem.aclose()
