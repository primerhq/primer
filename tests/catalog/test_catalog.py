"""Unit tests for primer.catalog.catalog.SemanticCatalog."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pytest

from primer.catalog import SemanticCatalog, SemanticEntityType
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Tool
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.embedding import EmbedResponse, Embedding
from primer.model.except_ import BadRequestError, ConfigError, NotFoundError
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.vector import EmbeddingRecord, SearchResult


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeEmbedder:
    """Deterministic embedder: vectors are hash-derived from input text."""

    def __init__(self, *, dimensions: int = 8) -> None:
        self._dimensions = dimensions
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    async def embed(self, *, model, inputs, **kwargs):
        self.calls.append({"model": model, "inputs": inputs, **kwargs})
        embeddings: list[Embedding] = []
        for i, part in enumerate(inputs):
            text = getattr(part, "text", "")
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Two bytes per dimension mapped to a float in [-1, 1] for
            # stable variation between distinct inputs.
            vector = [
                (
                    (
                        digest[(2 * j) % len(digest)] << 8
                        | digest[(2 * j + 1) % len(digest)]
                    )
                    / 32768.0
                )
                - 1.0
                for j in range(self._dimensions)
            ]
            embeddings.append(Embedding(index=i, vector=vector))
        return EmbedResponse(model=model, embeddings=embeddings, usage=None)


class _FakeVectorStore:
    """In-memory vector store keyed by (collection_id, document_id)."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        # collection_id -> {document_id: EmbeddingRecord}
        self._records: dict[str, dict[str, EmbeddingRecord]] = {}
        self.create_collection_calls: list[dict[str, Any]] = []

    async def create_collection(
        self,
        collection_id,
        *,
        dimensions,
        distance="cosine",
    ):
        self.create_collection_calls.append(
            {
                "collection_id": collection_id,
                "dimensions": dimensions,
                "distance": distance,
            }
        )
        if collection_id not in self.collections:
            self.collections[collection_id] = {
                "dimensions": dimensions,
                "distance": distance,
            }
            self._records.setdefault(collection_id, {})

    async def put(self, record: EmbeddingRecord):
        self._records.setdefault(record.collection_id, {})
        self._records[record.collection_id][record.document_id] = record

    async def search(self, collection_id, vector, k):
        records = self._records.get(collection_id, {})

        def _cos(a, b):
            num = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            if na == 0 or nb == 0:
                return 0.0
            return num / (na * nb)

        scored = [
            SearchResult(record=r, score=_cos(vector, r.vector))
            for r in records.values()
        ]
        scored.sort(key=lambda h: h.score or 0.0, reverse=True)
        return scored[:k]

    async def search_by_meta(self, *args, **kwargs):
        return []

    async def get(self, collection_id, document_id):
        records = self._records.get(collection_id, {})
        out = []
        if document_id in records:
            out.append(records[document_id])
        return out

    async def delete(self, collection_id, document_id):
        records = self._records.get(collection_id, {})
        records.pop(document_id, None)


class _FakeCollectionStorage:
    """In-memory Storage[Collection] shim."""

    def __init__(self) -> None:
        self._data: dict[str, Collection] = {}
        self.create_calls: list[Collection] = []

    async def get(self, id):
        return self._data.get(id)

    async def create(self, entity: Collection) -> Collection:
        self._data[entity.id] = entity
        self.create_calls.append(entity)
        return entity

    async def update(self, entity: Collection) -> Collection:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id):
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, *args, **kwargs):
        raise NotImplementedError

    async def find(self, *args, **kwargs):
        raise NotImplementedError


# ===========================================================================
# Helpers
# ===========================================================================


def _make_catalog() -> tuple[
    SemanticCatalog, _FakeEmbedder, _FakeVectorStore, _FakeCollectionStorage
]:
    embedder = _FakeEmbedder()
    vstore = _FakeVectorStore()
    storage = _FakeCollectionStorage()
    catalog = SemanticCatalog(
        embedder=embedder,  # type: ignore[arg-type]
        embedder_provider_id="p1",
        embedder_model="m1",
        vector_store=vstore,  # type: ignore[arg-type]
        collection_storage=storage,  # type: ignore[arg-type]
        search_provider_id="ssp-test",
    )
    return catalog, embedder, vstore, storage


def _agent(
    agent_id: str = "code-reviewer",
    description: str = "Reviews source code",
) -> Agent:
    return Agent(
        id=agent_id,
        description=description,
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _tool(
    tool_id: str,
    *,
    toolset_id: str,
    description: str = "a tool",
) -> Tool:
    return Tool(
        id=tool_id,
        description=description,
        toolset_id=toolset_id,
        args_schema={"type": "object"},
    )


def _graph(
    graph_id: str = "research",
    description: str = "Multi-stage research",
) -> Graph:
    return Graph(
        id=graph_id,
        description=description,
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="end"),
        ],
    )


def _collection(
    coll_id: str = "kb-1",
    description: str = "Knowledge base",
) -> Collection:
    return Collection(
        id=coll_id,
        description=description,
        embedder=CollectionEmbedder(provider_id="p", model="m"),
        search_provider_id="ssp-test",
    )


# ===========================================================================
# Construction guards
# ===========================================================================


class TestConstruction:
    def test_rejects_empty_provider_id(self) -> None:
        with pytest.raises(ConfigError, match="embedder_provider_id"):
            SemanticCatalog(
                embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                embedder_provider_id="",
                embedder_model="m1",
                vector_store=_FakeVectorStore(),  # type: ignore[arg-type]
                collection_storage=_FakeCollectionStorage(),  # type: ignore[arg-type]
                search_provider_id="ssp-test",
            )

    def test_rejects_empty_model_name(self) -> None:
        with pytest.raises(ConfigError, match="embedder_model"):
            SemanticCatalog(
                embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                embedder_provider_id="p1",
                embedder_model="",
                vector_store=_FakeVectorStore(),  # type: ignore[arg-type]
                collection_storage=_FakeCollectionStorage(),  # type: ignore[arg-type]
                search_provider_id="ssp-test",
            )

    def test_rejects_empty_search_provider_id(self) -> None:
        with pytest.raises(ConfigError, match="search_provider_id"):
            SemanticCatalog(
                embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                embedder_provider_id="p1",
                embedder_model="m1",
                vector_store=_FakeVectorStore(),  # type: ignore[arg-type]
                collection_storage=_FakeCollectionStorage(),  # type: ignore[arg-type]
                search_provider_id="",
            )


# ===========================================================================
# Initialize
# ===========================================================================


class TestInitialize:
    @pytest.mark.asyncio
    async def test_creates_four_system_collection_rows(self) -> None:
        catalog, _, vstore, storage = _make_catalog()
        await catalog.initialize()
        assert sorted(storage._data.keys()) == [
            "_catalog_agents",
            "_catalog_collections",
            "_catalog_graphs",
            "_catalog_tools",
        ]
        for coll in storage._data.values():
            assert coll.system is True
            assert coll.embedder.provider_id == "p1"
            assert coll.embedder.model == "m1"
        # Vector store collections also created with the same ids.
        assert sorted(vstore.collections.keys()) == [
            "_catalog_agents",
            "_catalog_collections",
            "_catalog_graphs",
            "_catalog_tools",
        ]

    @pytest.mark.asyncio
    async def test_idempotent(self) -> None:
        catalog, _, vstore, storage = _make_catalog()
        await catalog.initialize()
        first_create_count = len(storage.create_calls)
        first_vstore_count = len(vstore.create_collection_calls)
        await catalog.initialize()
        # No second create() on storage (rows already exist).
        assert len(storage.create_calls) == first_create_count
        # VectorStore.create_collection MAY be called again per its own
        # idempotent contract; assert it didn't grow uncontrollably.
        assert len(vstore.create_collection_calls) <= first_vstore_count + 4

    @pytest.mark.asyncio
    async def test_refuses_to_bind_non_system_row(self) -> None:
        catalog, _, _, storage = _make_catalog()
        # Pre-existing user collection occupying a reserved id.
        await storage.create(
            Collection(
                id="_catalog_agents",
                description="user squatting on the reserved id",
                embedder=CollectionEmbedder(provider_id="p", model="m"),
                search_provider_id="ssp-test",
                system=False,
            )
        )
        with pytest.raises(ConfigError, match="non-system"):
            await catalog.initialize()

    @pytest.mark.asyncio
    async def test_refuses_to_bind_system_row_with_different_embedder(
        self,
    ) -> None:
        catalog, _, _, storage = _make_catalog()
        # System row with a different provider — re-embedding belongs to
        # the activation API, not initialize().
        await storage.create(
            Collection(
                id="_catalog_agents",
                description="x",
                embedder=CollectionEmbedder(provider_id="other", model="other"),
                search_provider_id="ssp-test",
                system=True,
            )
        )
        with pytest.raises(ConfigError, match="provider/model"):
            await catalog.initialize()


# ===========================================================================
# Index
# ===========================================================================


class TestIndex:
    @pytest.mark.asyncio
    async def test_requires_initialize_first(self) -> None:
        catalog, _, _, _ = _make_catalog()
        with pytest.raises(ConfigError, match="initialize"):
            await catalog.index(SemanticEntityType.AGENT, _agent())

    @pytest.mark.asyncio
    async def test_indexes_agent_with_correct_record_shape(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        agent = _agent("code-reviewer", "Reviews source code")
        await catalog.index(SemanticEntityType.AGENT, agent)

        records = vstore._records["_catalog_agents"]
        assert "code-reviewer" in records
        record = records["code-reviewer"]
        assert record.text == "code-reviewer\n\nReviews source code"
        assert record.chunk_id == "0"
        assert record.meta == {"entity_type": "agent"}
        assert len(record.vector) == 8  # default fake embedder dimensionality

    @pytest.mark.asyncio
    async def test_index_is_upsert(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        await catalog.index(
            SemanticEntityType.AGENT,
            _agent(description="version 1"),
        )
        await catalog.index(
            SemanticEntityType.AGENT,
            _agent(description="version 2"),
        )
        record = vstore._records["_catalog_agents"]["code-reviewer"]
        assert "version 2" in record.text
        # Only one record per document_id.
        assert len(vstore._records["_catalog_agents"]) == 1

    @pytest.mark.asyncio
    async def test_index_tool_uses_scoped_id_when_present(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        # Tool already scoped (came from ToolExecutionManager).
        scoped_tool = _tool("web__web_search", toolset_id="web")
        await catalog.index(SemanticEntityType.TOOL, scoped_tool)
        assert "web__web_search" in vstore._records["_catalog_tools"]

    @pytest.mark.asyncio
    async def test_index_tool_composes_scope_when_bare(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        # Tool given with bare id (defence-in-depth: caller bypassing
        # ToolExecutionManager during a backfill, etc.).
        bare_tool = _tool("web_search", toolset_id="web")
        await catalog.index(SemanticEntityType.TOOL, bare_tool)
        assert "web__web_search" in vstore._records["_catalog_tools"]

    @pytest.mark.asyncio
    async def test_index_validates_entity_type_matches(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        with pytest.raises(BadRequestError, match="expects Agent"):
            await catalog.index(SemanticEntityType.AGENT, _graph())

    @pytest.mark.asyncio
    async def test_embedded_text_is_id_plus_description(self) -> None:
        catalog, embedder, _, _ = _make_catalog()
        await catalog.initialize()
        # Drain calls accumulated during the initialize() probe.
        embedder.calls.clear()
        await catalog.index(
            SemanticEntityType.AGENT,
            _agent("alice", "Does the thing."),
        )
        assert len(embedder.calls) == 1
        inputs = embedder.calls[0]["inputs"]
        assert len(inputs) == 1
        assert inputs[0].text == "alice\n\nDoes the thing."

    @pytest.mark.asyncio
    async def test_indexes_graph(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        await catalog.index(
            SemanticEntityType.GRAPH, _graph("rg-1", "Graph 1")
        )
        assert "rg-1" in vstore._records["_catalog_graphs"]

    @pytest.mark.asyncio
    async def test_indexes_collection(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        await catalog.index(
            SemanticEntityType.COLLECTION, _collection("kb-1")
        )
        assert "kb-1" in vstore._records["_catalog_collections"]


# ===========================================================================
# Delete
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_requires_initialize_first(self) -> None:
        catalog, _, _, _ = _make_catalog()
        with pytest.raises(ConfigError, match="initialize"):
            await catalog.delete(SemanticEntityType.AGENT, "a-1")

    @pytest.mark.asyncio
    async def test_removes_only_targeted_document(self) -> None:
        catalog, _, vstore, _ = _make_catalog()
        await catalog.initialize()
        a1 = _agent("a-1", "first")
        a2 = _agent("a-2", "second")
        await catalog.index(SemanticEntityType.AGENT, a1)
        await catalog.index(SemanticEntityType.AGENT, a2)
        await catalog.index(SemanticEntityType.GRAPH, _graph("rg-1"))

        await catalog.delete(SemanticEntityType.AGENT, "a-1")

        # a-1 gone; a-2 + graph rows untouched.
        assert "a-1" not in vstore._records["_catalog_agents"]
        assert "a-2" in vstore._records["_catalog_agents"]
        assert "rg-1" in vstore._records["_catalog_graphs"]

    @pytest.mark.asyncio
    async def test_empty_id_raises(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        with pytest.raises(BadRequestError, match="entity_id"):
            await catalog.delete(SemanticEntityType.AGENT, "")


# ===========================================================================
# Search
# ===========================================================================


class TestSearch:
    @pytest.mark.asyncio
    async def test_requires_initialize_first(self) -> None:
        catalog, _, _, _ = _make_catalog()
        with pytest.raises(ConfigError, match="initialize"):
            await catalog.search(SemanticEntityType.AGENT, "x", k=5)

    @pytest.mark.asyncio
    async def test_k_zero_raises(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        with pytest.raises(BadRequestError, match="k"):
            await catalog.search(SemanticEntityType.AGENT, "x", k=0)

    @pytest.mark.asyncio
    async def test_empty_query_raises(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        with pytest.raises(BadRequestError, match="query"):
            await catalog.search(SemanticEntityType.AGENT, "", k=5)

    @pytest.mark.asyncio
    async def test_returns_semantic_hits(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        await catalog.index(
            SemanticEntityType.AGENT,
            _agent("alice", "Reviews code carefully."),
        )
        await catalog.index(
            SemanticEntityType.AGENT,
            _agent("bob", "Writes documentation."),
        )

        hits = await catalog.search(SemanticEntityType.AGENT, "alice", k=2)
        assert len(hits) == 2
        for h in hits:
            assert h.entity_type is SemanticEntityType.AGENT
            assert h.entity_id in {"alice", "bob"}
            assert "\n\n" in h.text  # id + description format

    @pytest.mark.asyncio
    async def test_only_searches_one_collection(self) -> None:
        catalog, _, _, _ = _make_catalog()
        await catalog.initialize()
        await catalog.index(SemanticEntityType.AGENT, _agent("a-1"))
        await catalog.index(SemanticEntityType.GRAPH, _graph("g-1"))
        # Searching agents must not return graphs.
        hits = await catalog.search(SemanticEntityType.AGENT, "anything", k=5)
        assert all(h.entity_type is SemanticEntityType.AGENT for h in hits)
        assert {h.entity_id for h in hits} == {"a-1"}
