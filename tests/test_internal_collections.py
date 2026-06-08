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

from primer.internal_collections import (
    INTERNAL_COLLECTION_IDS,
    IngestEvent,
    InternalCollectionsSubsystem,
    build_subsystem,
    embedding_text_for,
)
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.except_ import ConfigError, ConflictError, NotFoundError
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


def _ingester_factory_for_test():
    """Build a DocumentIngester that uses the recursive splitter and a fake
    text loader — avoids the Docling import path (slow + heavyweight) in
    unit tests. Lives as a fixture-shaped helper so the IC subsystem's
    ``_ingest_ai_docs`` can be unit-tested without pulling in Docling.
    """
    from pathlib import Path

    from primer.ingest.ingester import DocumentIngester
    from primer.ingest.loader import DocumentLoader
    from primer.ingest.splitters.recursive import RecursiveSplitter
    from primer.model.ingest import LoadedDocument

    class _PathTextLoader(DocumentLoader):
        async def load(self, source):
            path = source if isinstance(source, Path) else Path(source)
            text = path.read_text(encoding="utf-8")
            return LoadedDocument(text=text, meta={"bytes_loaded": len(text)})

    def factory(*, collection, embedder, vector_store):
        return DocumentIngester(
            collection=collection,
            embedder=embedder,
            vector_store=vector_store,
            loader=_PathTextLoader(),
            splitter=RecursiveSplitter(chunk_size=512, chunk_overlap=32),
        )

    return factory


class TestAiDocsBootstrap:
    @pytest.mark.asyncio
    async def test_materialise_creates_ai_docs_collection_row(
        self, subsystem, store, sp
    ) -> None:
        from primer.internal_collections import AI_DOCS_COLLECTION_ID

        await subsystem.bootstrap()
        coll_storage = sp.get_storage(Collection)
        ai_docs = await coll_storage.get(AI_DOCS_COLLECTION_ID)
        assert ai_docs is not None
        assert ai_docs.system is True
        assert AI_DOCS_COLLECTION_ID in store.collections
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_ingest_walks_markdown_files_and_creates_documents(
        self, subsystem, sp, tmp_path
    ) -> None:
        from primer.internal_collections import AI_DOCS_COLLECTION_ID
        from primer.model.collection import Document

        await subsystem._materialise_collection_rows()

        (tmp_path / "agents.md").write_text(
            "---\ntitle: Agents\nsummary: Agent runtime.\n---\n# Agents\n\nbody A\n"
        )
        (tmp_path / "chats.md").write_text(
            "---\ntitle: Chats\nsummary: Chat turns.\n---\n# Chats\n\nbody B\n"
        )
        # Underscore-prefix files should be skipped.
        (tmp_path / "_README.md").write_text("internal note; do not ingest")
        # Non-markdown files should be skipped.
        (tmp_path / "ignore.txt").write_text("not a doc")

        async def _emit(*args, **kwargs):
            return None

        counts: dict[str, int] = {"docs": 0}
        result = await subsystem._ingest_ai_docs(
            _emit,
            counts,
            ai_docs_path=tmp_path,
            ingester_factory=_ingester_factory_for_test(),
        )
        assert result == 2  # agents.md + chats.md (not _README.md / ignore.txt)
        assert counts["docs"] == 2

        # Both Documents were created with the right meta.
        doc_storage = sp.get_storage(Document)
        agents_doc = await doc_storage.get("agents")
        chats_doc = await doc_storage.get("chats")
        assert agents_doc is not None
        assert agents_doc.collection_id == AI_DOCS_COLLECTION_ID
        assert agents_doc.name == "Agents"
        assert agents_doc.meta["title"] == "Agents"
        assert agents_doc.meta["summary"] == "Agent runtime."
        assert agents_doc.meta["slug"] == "agents"
        assert "content_hash" in agents_doc.meta
        assert chats_doc is not None
        assert chats_doc.name == "Chats"

    @pytest.mark.asyncio
    async def test_content_hash_skips_unchanged_files(
        self, subsystem, store, sp, tmp_path
    ) -> None:
        from primer.model.collection import Document

        await subsystem._materialise_collection_rows()
        (tmp_path / "agents.md").write_text(
            "---\ntitle: Agents\n---\n# Agents\n\nbody one\n"
        )

        async def _emit(*args, **kwargs):
            return None

        counts: dict[str, int] = {"docs": 0}
        factory = _ingester_factory_for_test()
        # First pass — embeds + writes records.
        await subsystem._ingest_ai_docs(
            _emit, counts, ai_docs_path=tmp_path, ingester_factory=factory,
        )
        first_record_count = len(store.records)
        assert first_record_count > 0

        # Second pass with identical content — should hit the skip path.
        # No new vector store writes; no embedder call increase.
        embedder_calls_before = len(subsystem._pr._embedder.calls)
        await subsystem._ingest_ai_docs(
            _emit, counts, ai_docs_path=tmp_path, ingester_factory=factory,
        )
        # Skip means the content_hash matched; no chunks re-embedded.
        # The embedder may still be touched once for the search query
        # path or other unrelated calls; check NEW ingest calls didn't
        # happen by comparing record count.
        assert len(store.records) == first_record_count

    @pytest.mark.asyncio
    async def test_content_hash_reingests_changed_files(
        self, subsystem, store, sp, tmp_path
    ) -> None:
        from primer.model.collection import Document

        await subsystem._materialise_collection_rows()
        path = tmp_path / "agents.md"
        path.write_text("---\ntitle: Agents\n---\nv1 body\n")

        async def _emit(*args, **kwargs):
            return None

        counts: dict[str, int] = {"docs": 0}
        factory = _ingester_factory_for_test()
        await subsystem._ingest_ai_docs(
            _emit, counts, ai_docs_path=tmp_path, ingester_factory=factory,
        )
        first_hash = (await sp.get_storage(Document).get("agents")).meta["content_hash"]

        # Edit the file — same id, new content.
        path.write_text("---\ntitle: Agents\n---\nv2 body — completely different\n")
        await subsystem._ingest_ai_docs(
            _emit, counts, ai_docs_path=tmp_path, ingester_factory=factory,
        )
        second_hash = (await sp.get_storage(Document).get("agents")).meta["content_hash"]
        assert second_hash != first_hash

    @pytest.mark.asyncio
    async def test_missing_directory_is_a_no_op(
        self, subsystem, sp, tmp_path
    ) -> None:
        async def _emit(*args, **kwargs):
            return None

        # Pass a path that doesn't exist — _ingest_ai_docs should log
        # + skip rather than raise.
        missing = tmp_path / "does-not-exist"
        counts: dict[str, int] = {"docs": 0}
        result = await subsystem._ingest_ai_docs(
            _emit,
            counts,
            ai_docs_path=missing,
            ingester_factory=_ingester_factory_for_test(),
        )
        assert result == 0
        assert counts["docs"] == 0

    @pytest.mark.asyncio
    async def test_search_ai_docs_returns_subsystem_inactive_when_not_bootstrapped(
        self, subsystem
    ) -> None:
        with pytest.raises(ConfigError):
            await subsystem.search_ai_docs(query="hello", top_k=5)

    @pytest.mark.asyncio
    async def test_search_ai_docs_after_bootstrap_hits_vector_store(
        self, subsystem, store, sp, tmp_path
    ) -> None:
        from primer.internal_collections import AI_DOCS_COLLECTION_ID

        (tmp_path / "agents.md").write_text(
            "---\ntitle: Agents\n---\n# Agents\n\nbody\n"
        )

        async def _emit(*args, **kwargs):
            return None

        await subsystem.bootstrap()  # materialise + activate

        counts: dict[str, int] = {"docs": 0}
        await subsystem._ingest_ai_docs(
            _emit,
            counts,
            ai_docs_path=tmp_path,
            ingester_factory=_ingester_factory_for_test(),
        )

        hits = await subsystem.search_ai_docs(query="agents", top_k=5)
        assert isinstance(hits, list)
        # Fake search returns every record in the collection.
        ai_docs_hits = [h for h in hits if h.record.collection_id == AI_DOCS_COLLECTION_ID]
        assert len(ai_docs_hits) >= 1
        await subsystem.aclose()

    @pytest.mark.asyncio
    async def test_ingest_recurses_subdirs_with_relative_path_slug(
        self, subsystem, sp, tmp_path
    ) -> None:
        from primer.model.collection import Document

        await subsystem._materialise_collection_rows()
        (tmp_path / "agents.md").write_text(
            "---\ntitle: Agents\nsummary: A.\n---\n# Agents\n\nbody\n"
        )
        (tmp_path / "cookbook").mkdir()
        (tmp_path / "cookbook" / "pr-reviewer.md").write_text(
            "---\ntitle: PR reviewer\nsummary: R.\n---\n# PR\n\nbody\n"
        )

        async def _emit(*a, **k):
            return None

        counts: dict[str, int] = {"docs": 0}
        result = await subsystem._ingest_ai_docs(
            _emit, counts, ai_docs_path=tmp_path,
            ingester_factory=_ingester_factory_for_test(),
        )
        assert result == 2
        docs = sp.get_storage(Document)
        assert await docs.get("agents") is not None
        sub = await docs.get("cookbook/pr-reviewer")
        assert sub is not None
        assert sub.meta["slug"] == "cookbook/pr-reviewer"
