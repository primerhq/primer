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
from unittest.mock import patch

import pytest

from primer.internal_collections import (
    INTERNAL_COLLECTION_IDS,
    IngestEvent,
    InternalCollectionsSubsystem,
    build_subsystem,
    embedding_text_for,
)
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.except_ import (
    ConfigError,
    ConflictError,
    DimensionMismatchError,
    NotFoundError,
)
from primer.model.internal import (
    INTERNAL_COLLECTIONS_CONFIG_ID,
    IngestFailure,
    InternalCollectionsConfig,
)
from primer.model.storage import OffsetPage, OffsetPageResponse


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

    async def update_unless(
        self,
        e,
        *,
        field,
        forbidden,
        conn=None,
    ):
        current = self._data.get(e.id)
        if current is None:
            raise NotFoundError(f"no entity with id {e.id!r}")
        if getattr(current, field, None) == forbidden:
            return None
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
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

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

    async def create_collection(self, collection_id, *, dimensions, distance="cosine"):
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

    async def drop_collection(self, collection_id):
        self.collections.pop(collection_id, None)
        for key in list(self.records.keys()):
            if key[0] == collection_id:
                del self.records[key]

    async def search(self, collection_id, vector, k):
        self.searches.append((collection_id, list(vector), k))
        from primer.model.vector import SearchResult

        hits = []
        for (cid, _, _), record in self.records.items():
            if cid != collection_id:
                continue
            hits.append(SearchResult(record=record, score=1.0))
        return hits[:k]


class _FakeSSR:
    """Fake SemanticSearchRegistry: returns the given store for any ssp_id."""

    def __init__(self, store: _FakeVectorStore) -> None:
        self._store = store
        self.is_configured = True

    async def get_store(self, ssp_id: str):
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
            def __init__(self, vecs):
                self.embeddings = [type("E", (), {"vector": v})() for v in vecs]

        vecs = [
            [float((hash(inp.text) >> i) & 0xFF) / 255.0 for i in range(self.dim)]
            for inp in inputs
        ]
        return _R(vecs)


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


@pytest.fixture(autouse=True)
def _stub_ingest_ai_docs(request):
    """Stub bootstrap()'s AI-docs ingest to a no-op.

    bootstrap() defaults to the production DocumentIngester (Docling + the
    sentence-transformers OCR stack) walking ~30 real markdown files, which
    costs 7-10s per call and dominated this file's runtime. No bootstrap-path
    test asserts the doc-walk output, so stub it out — exactly as
    tests/api/test_internal_collections.py already does.

    TestAiDocsBootstrap exercises the real ingest seam directly with a fast
    in-test ingester_factory, so it must run un-stubbed — EXCEPT
    test_materialise_creates_ai_docs_collection_row, which calls bootstrap()
    (and only asserts the collection row), so it keeps the stub.
    """
    cls = request.cls
    real_ingest = (
        cls is not None
        and cls.__name__ == "TestAiDocsBootstrap"
        and request.function.__name__
        != "test_materialise_creates_ai_docs_collection_row"
    )
    if real_ingest:
        yield
        return

    async def _noop(self, emit, counts, **kwargs):
        counts["docs"] = 0
        return 0

    with patch(
        "primer.internal_collections.InternalCollectionsSubsystem._ingest_ai_docs",
        new=_noop,
    ):
        yield


@pytest.fixture
def cfg() -> InternalCollectionsConfig:
    return InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID,
        embedding_provider_id="hf-1",
        embedding_model="all-MiniLM-L6-v2",
        search_provider_id="ssp-test",
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
def ssr(store) -> _FakeSSR:
    return _FakeSSR(store)


@pytest.fixture
def subsystem(cfg, sp, pr, ssr) -> InternalCollectionsSubsystem:
    return build_subsystem(
        config=cfg,
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,  # type: ignore[arg-type]
        semantic_search_registry=ssr,  # type: ignore[arg-type]
    )


def _agent(id="agt-1") -> Agent:
    return Agent(
        id=id,
        description="research agent that finds papers",
        model=AgentModel(profile_id="anthropic-1--claude-sonnet-4-6"),
        tools=[],
        system_prompt=["you find scientific papers"],
    )


def _collection(id="kb-1") -> Collection:
    return Collection(
        id=id,
        description="knowledge base of articles",
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
    async def test_bootstrap_ingests_collections(self, subsystem, store, sp) -> None:
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
        self, cfg, sp, pr, ssr, store
    ) -> None:
        from primer.model.chat import Tool

        class _ToolsetProvider:
            async def list_tools(self, principal=None):
                for tn in ("foo", "bar"):
                    yield Tool(
                        id=tn,
                        toolset_id="system",
                        description=f"{tn} description",
                        args_schema={"type": "object"},
                    )

        subsystem = build_subsystem(
            config=cfg,
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=pr,  # type: ignore[arg-type]
            semantic_search_registry=ssr,  # type: ignore[arg-type]
            toolset_providers={"system": _ToolsetProvider()},
        )
        result = await subsystem.bootstrap()
        assert result["counts"]["tools"] == 2
        tools_coll = INTERNAL_COLLECTION_IDS["tool"]
        assert (tools_coll, "system::foo", "0") in store.records
        assert (tools_coll, "system::bar", "0") in store.records
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_rebootstrap_purges_renamed_tool_docs(
        self, cfg, sp, pr, ssr, store
    ) -> None:
        """A re-bootstrap must not leave orphaned tool docs behind when a
        tool's scoped id changes (moved to a new toolset or renamed). The
        tools collection is fully regenerated from the live registry, so
        bootstrap drops + recreates it rather than upserting on top of the
        stale catalog."""
        from primer.model.chat import Tool

        class _RenamingProvider:
            def __init__(self) -> None:
                self.names = ("foo", "bar")

            async def list_tools(self, principal=None):
                for tn in self.names:
                    yield Tool(
                        id=tn,
                        toolset_id="system",
                        description=f"{tn} description",
                        args_schema={"type": "object"},
                    )

        provider = _RenamingProvider()
        subsystem = build_subsystem(
            config=cfg,
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=pr,  # type: ignore[arg-type]
            semantic_search_registry=ssr,  # type: ignore[arg-type]
            toolset_providers={"system": provider},
        )
        tools_coll = INTERNAL_COLLECTION_IDS["tool"]

        await subsystem.bootstrap()
        assert (tools_coll, "system::bar", "0") in store.records

        # "bar" is renamed to "baz" (e.g. moved to another toolset); the
        # live registry no longer yields the old id.
        provider.names = ("foo", "baz")
        await subsystem.bootstrap()

        assert (tools_coll, "system::foo", "0") in store.records
        assert (tools_coll, "system::baz", "0") in store.records
        # The stale id must be gone, not orphaned alongside the new one.
        assert (tools_coll, "system::bar", "0") not in store.records
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_bootstrap_raises_dimension_mismatch_error_when_store_has_different_dim(
        self, cfg, sp, pr, ssr
    ) -> None:
        """Bootstrap raises DimensionMismatchError (422) when the vector store
        already has an internal collection at a different dimension than the
        active embedder produces."""

        class _MismatchStore(_FakeVectorStore):
            """Store where every internal collection was already created at dim=9999."""

            async def create_collection(
                self, collection_id, *, dimensions, distance="cosine"
            ):
                # Simulate the store already holding this collection at a
                # different dimension by raising ConflictError (same message
                # format as the pgvector backend).
                if collection_id in INTERNAL_COLLECTION_IDS.values():
                    raise ConflictError(
                        f"collection {collection_id!r} already exists with "
                        f"dimensions=9999, distance='cosine'; "
                        f"requested dimensions={dimensions}, distance='cosine'"
                    )
                await super().create_collection(
                    collection_id, dimensions=dimensions, distance=distance
                )

        mismatch_ssr = _FakeSSR(_MismatchStore())
        subsystem = build_subsystem(
            config=cfg,
            storage_provider=sp,
            provider_registry=pr,
            semantic_search_registry=mismatch_ssr,
        )
        with pytest.raises(DimensionMismatchError) as exc_info:
            await subsystem.bootstrap()

        err = exc_info.value
        assert err.status_code == 422
        # The stored dim comes from the ConflictError message.
        assert err.collection_dim == 9999
        # The embedder dim is 4 (the _FakeEmbedder default).
        assert err.embedder_dim == 4


# ===========================================================================
# CDC worker
# ===========================================================================


class TestCDCWorker:
    @pytest.mark.asyncio
    async def test_enqueue_then_worker_applies_upsert(self, subsystem, store) -> None:
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
    async def test_enqueue_then_worker_applies_delete(self, subsystem, store) -> None:
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


# ===========================================================================
# InternalCollectionsConfig model field tests
# ===========================================================================


def test_internal_collections_config_requires_search_provider_id():
    import pytest
    from pydantic import ValidationError
    from primer.model.internal import InternalCollectionsConfig

    with pytest.raises(ValidationError):
        InternalCollectionsConfig(
            id="_internal_collections_config",
            embedding_provider_id="emb-1",
            embedding_model="m1",
            # no search_provider_id
        )


def test_internal_collections_config_with_search_provider_id_constructs():
    from primer.model.internal import InternalCollectionsConfig

    cfg = InternalCollectionsConfig(
        id="_internal_collections_config",
        embedding_provider_id="emb-1",
        embedding_model="m1",
        search_provider_id="ssp-1",
    )
    assert cfg.search_provider_id == "ssp-1"


# ===========================================================================
# AI docs ingest (5th reserved collection)
# ===========================================================================


# The ai-docs ingest class went with primer/ingest: the shipped docs are
# written into the system collection as tree documents now, covered by
# tests/knowledge/test_system_collection.py.
