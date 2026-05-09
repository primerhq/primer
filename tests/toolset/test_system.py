"""End-to-end tests for the ``_system`` internal toolset.

Verifies the catalog assembles, every CRUD set is wired, mutation
cascades fire, the meta tools (``call_tool``, ``list_toolset_tools``)
work, threads CRUD is exposed, and deferred stubs surface their
``not-implemented`` payload cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from matrix.api.registries import ProviderRegistry, VectorStoreRegistry
from matrix.model.agent import Agent, AgentModel
from matrix.model.collection import Collection, CollectionEmbedder
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.provider import (
    AnthropicConfig,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)
from matrix.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
)
from matrix.model.thread import Thread
from matrix.toolset.system import SYSTEM_TOOLSET_ID, build_system_toolset


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
        from matrix.model.storage import OffsetPage

        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor = (
            str(offset + page.length) if offset + page.length < len(items) else None
        )
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp: _SP) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def vsr(sp: _SP) -> VectorStoreRegistry:
    return VectorStoreRegistry(sp, factory=lambda c: object())  # type: ignore[arg-type]


@pytest.fixture
def system_toolset(sp: _SP, pr: ProviderRegistry, vsr: VectorStoreRegistry):
    provider = build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        vector_store_registry=vsr,
    )
    pr._system_toolset_provider = provider  # type: ignore[attr-defined]
    return provider


def _llm() -> LLMProvider:
    return LLMProvider(
        id="anthropic-1",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _emb() -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="sentence-transformers/all-MiniLM-L6-v2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


def _agent() -> Agent:
    return Agent(
        id="agt-1",
        description="test agent",
        model=AgentModel(provider_id="anthropic-1", model_name="claude-sonnet-4-6"),
        temperature=0.0,
        tools=[],
        system_prompt=["you are a test"],
    )


def _collection() -> Collection:
    return Collection(
        id="kb-1",
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
    )


def _thread() -> Thread:
    now = datetime.now(timezone.utc)
    return Thread(
        id="th-1",
        agent_id="agt-1",
        title="hello",
        created_at=now,
        last_activity_at=now,
    )


# ===========================================================================
# Catalog tests
# ===========================================================================


class TestCatalog:
    @pytest.mark.asyncio
    async def test_toolset_id_is_reserved(self, system_toolset) -> None:
        assert SYSTEM_TOOLSET_ID == "_system"
        async for t in system_toolset.list_tools():
            assert t.toolset_id == SYSTEM_TOOLSET_ID

    @pytest.mark.asyncio
    async def test_catalog_has_expected_tools(self, system_toolset) -> None:
        names = {t.id async for t in system_toolset.list_tools()}
        for entity, plural in [
            ("llm_provider", "llm_providers"),
            ("embedding_provider", "embedding_providers"),
            ("cross_encoder_provider", "cross_encoder_providers"),
            ("toolset", "toolsets"),
            ("agent", "agents"),
            ("graph", "graphs"),
            ("collection", "collections"),
            ("document", "documents"),
            ("vector_store_config", "vector_store_configs"),
            ("agent_thread", "agent_threads"),
            ("graph_thread", "graph_threads"),
        ]:
            for verb in ("list", "get", "create", "update", "delete", "find"):
                expected = (
                    f"{verb}_{plural}"
                    if verb in ("list", "find")
                    else f"{verb}_{entity}"
                )
                assert expected in names, f"missing {expected}"
        assert "fetch_llm_provider_models" in names
        assert "fetch_embedding_provider_models" in names
        assert "fetch_cross_encoder_provider_models" in names
        assert "list_toolset_tools" in names
        assert "call_tool" in names
        assert "list_collection_documents" in names
        assert "find_collection_documents_by_meta" in names
        assert "search_collection" in names
        assert "refresh_collection" in names
        assert "get_document_content" in names
        assert "put_document" in names

    @pytest.mark.asyncio
    async def test_each_tool_has_non_empty_description(self, system_toolset) -> None:
        async for t in system_toolset.list_tools():
            assert t.description, f"empty description on {t.id}"
            assert len(t.description) > 30, f"too-thin description on {t.id}"
            assert isinstance(t.schema, dict)


# ===========================================================================
# Per-entity CRUD round-trips (LLMProvider as the canonical exemplar)
# ===========================================================================


class TestLLMProviderTools:
    @pytest.mark.asyncio
    async def test_create_get_update_delete_roundtrip(
        self, system_toolset
    ) -> None:
        body = _llm().model_dump(mode="json")

        result = await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        assert not result.is_error, result.output
        assert json.loads(result.output)["id"] == "anthropic-1"

        result = await system_toolset.call(
            tool_name="get_llm_provider", arguments={"id": "anthropic-1"}
        )
        assert not result.is_error
        assert json.loads(result.output)["id"] == "anthropic-1"

        body["limits"]["max_concurrency"] = 8
        result = await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"id": "anthropic-1", "entity": body},
        )
        assert not result.is_error
        assert json.loads(result.output)["limits"]["max_concurrency"] == 8

        result = await system_toolset.call(
            tool_name="list_llm_providers", arguments={}
        )
        assert not result.is_error
        page = json.loads(result.output)
        assert page["total"] == 1
        assert page["items"][0]["id"] == "anthropic-1"

        result = await system_toolset.call(
            tool_name="find_llm_providers", arguments={"predicate": None}
        )
        assert not result.is_error
        assert json.loads(result.output)["length"] == 1

        result = await system_toolset.call(
            tool_name="delete_llm_provider", arguments={"id": "anthropic-1"}
        )
        assert not result.is_error
        assert json.loads(result.output) == {"deleted": True, "id": "anthropic-1"}

        result = await system_toolset.call(
            tool_name="get_llm_provider", arguments={"id": "anthropic-1"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_conflict(self, system_toolset) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        result = await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_update_id_mismatch_returns_conflict(self, system_toolset) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        result = await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"id": "different", "entity": body},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_create_invalid_body_returns_validation_error(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="create_llm_provider",
            arguments={"entity": {"id": "x"}},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"


# ===========================================================================
# Cascade invalidation
# ===========================================================================


class TestCascadeInvalidation:
    @pytest.mark.asyncio
    async def test_update_invalidates_cached_llm_adapter(
        self, system_toolset, pr
    ) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )

        sentinel_v1 = MagicMock()
        sentinel_v1.aclose = AsyncMock()
        sentinel_v2 = MagicMock()
        sentinel_v2.aclose = AsyncMock()

        pr._llm_factory = lambda p: sentinel_v1  # type: ignore[attr-defined]
        first = await pr.get_llm("anthropic-1")
        assert first is sentinel_v1

        pr._llm_factory = lambda p: sentinel_v2  # type: ignore[attr-defined]
        body["limits"]["max_concurrency"] = 8
        await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"id": "anthropic-1", "entity": body},
        )

        second = await pr.get_llm("anthropic-1")
        assert second is sentinel_v2
        sentinel_v1.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_vector_store_config_invalidates_registry(
        self, system_toolset, vsr
    ) -> None:
        from matrix.api.registries.vector_store_registry import (
            ACTIVE_VECTOR_STORE_CONFIG_ID,
        )
        from matrix.model.vector import VectorStoreConfig

        body = VectorStoreConfig(
            id=ACTIVE_VECTOR_STORE_CONFIG_ID,
            backend="pgvector",
            settings={"dsn": "x"},
        ).model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_vector_store_config", arguments={"entity": body}
        )
        sentinel = MagicMock()
        sentinel.aclose = AsyncMock()
        vsr._provider = sentinel  # type: ignore[attr-defined]
        vsr._store = MagicMock()  # type: ignore[attr-defined]

        await system_toolset.call(
            tool_name="delete_vector_store_config",
            arguments={"id": ACTIVE_VECTOR_STORE_CONFIG_ID},
        )
        assert vsr._provider is None  # type: ignore[attr-defined]
        sentinel.aclose.assert_awaited_once()


# ===========================================================================
# Provider extras: fetch_models
# ===========================================================================


class TestFetchModels:
    @pytest.mark.asyncio
    async def test_fetch_llm_provider_models(self, system_toolset, pr) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        adapter = MagicMock()
        adapter.list_models = AsyncMock(return_value=["claude-sonnet-4-6"])
        adapter.aclose = AsyncMock()
        pr._llm_factory = lambda p: adapter  # type: ignore[attr-defined]

        result = await system_toolset.call(
            tool_name="fetch_llm_provider_models",
            arguments={"provider_id": "anthropic-1"},
        )
        assert not result.is_error
        assert json.loads(result.output) == {"models": ["claude-sonnet-4-6"]}

    @pytest.mark.asyncio
    async def test_fetch_models_returns_not_found_when_missing(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="fetch_llm_provider_models",
            arguments={"provider_id": "missing"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"


# ===========================================================================
# Toolset extras: list_toolset_tools, call_tool
# ===========================================================================


class TestToolsetExtras:
    @pytest.mark.asyncio
    async def test_list_toolset_tools_can_introspect_self(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="list_toolset_tools",
            arguments={"toolset_id": SYSTEM_TOOLSET_ID},
        )
        assert not result.is_error
        body = json.loads(result.output)
        names = {t["id"] for t in body["tools"]}
        assert "list_toolset_tools" in names
        assert "call_tool" in names

    @pytest.mark.asyncio
    async def test_call_tool_dispatches_to_self(self, system_toolset) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": SYSTEM_TOOLSET_ID,
                "tool_name": "get_llm_provider",
                "arguments": {"id": "anthropic-1"},
            },
        )
        assert not result.is_error
        assert json.loads(result.output)["id"] == "anthropic-1"

    @pytest.mark.asyncio
    async def test_call_tool_propagates_inner_error(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": SYSTEM_TOOLSET_ID,
                "tool_name": "get_llm_provider",
                "arguments": {"id": "missing"},
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"


# ===========================================================================
# Threads CRUD (Agent thread is the exemplar)
# ===========================================================================


class TestAgentThreads:
    @pytest.mark.asyncio
    async def test_create_then_get_thread(self, system_toolset) -> None:
        body = _thread().model_dump(mode="json")
        result = await system_toolset.call(
            tool_name="create_agent_thread", arguments={"entity": body}
        )
        assert not result.is_error, result.output
        result = await system_toolset.call(
            tool_name="get_agent_thread", arguments={"id": "th-1"}
        )
        assert not result.is_error
        assert json.loads(result.output)["agent_id"] == "agt-1"


# ===========================================================================
# Collection extras + deferred stubs
# ===========================================================================


class TestCollectionExtras:
    @pytest.mark.asyncio
    async def test_list_collection_documents(self, system_toolset) -> None:
        await system_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        await system_toolset.call(
            tool_name="put_document",
            arguments={
                "id": "doc-1",
                "collection_id": "kb-1",
                "name": "hello.txt",
                "content": "hello world",
            },
        )
        result = await system_toolset.call(
            tool_name="list_collection_documents",
            arguments={"collection_id": "kb-1"},
        )
        assert not result.is_error
        assert json.loads(result.output)["length"] >= 1

    @pytest.mark.asyncio
    async def test_list_collection_documents_404(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="list_collection_documents",
            arguments={"collection_id": "missing"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_search_collection_returns_not_implemented(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="search_collection",
            arguments={"collection_id": "kb-1", "query": "anything", "top_k": 5},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-implemented"

    @pytest.mark.asyncio
    async def test_refresh_collection_returns_not_implemented_when_collection_exists(
        self, system_toolset
    ) -> None:
        await system_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        result = await system_toolset.call(
            tool_name="refresh_collection",
            arguments={"collection_id": "kb-1"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-implemented"


# ===========================================================================
# Document extras: put_document + get_document_content round-trip
# ===========================================================================


class TestDocumentExtras:
    @pytest.mark.asyncio
    async def test_put_then_get_content(self, system_toolset) -> None:
        await system_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        result = await system_toolset.call(
            tool_name="put_document",
            arguments={
                "id": "doc-1",
                "collection_id": "kb-1",
                "name": "hello.txt",
                "content": "this is the content",
                "meta": {"author": "alice"},
            },
        )
        assert not result.is_error
        result = await system_toolset.call(
            tool_name="get_document_content", arguments={"document_id": "doc-1"}
        )
        assert not result.is_error
        body = json.loads(result.output)
        assert body["content"] == "this is the content"
        assert body["name"] == "hello.txt"

    @pytest.mark.asyncio
    async def test_put_document_upserts(self, system_toolset) -> None:
        await system_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        for content in ("first", "second"):
            result = await system_toolset.call(
                tool_name="put_document",
                arguments={
                    "id": "doc-1",
                    "collection_id": "kb-1",
                    "name": "x.txt",
                    "content": content,
                },
            )
            assert not result.is_error
        result = await system_toolset.call(
            tool_name="get_document_content", arguments={"document_id": "doc-1"}
        )
        assert json.loads(result.output)["content"] == "second"


# ===========================================================================
# Pagination contract
# ===========================================================================


class TestPagination:
    @pytest.mark.asyncio
    async def test_default_pagination(self, system_toolset) -> None:
        for i in range(3):
            body = _llm().model_dump(mode="json")
            body["id"] = f"row-{i}"
            await system_toolset.call(
                tool_name="create_llm_provider", arguments={"entity": body}
            )
        result = await system_toolset.call(
            tool_name="list_llm_providers", arguments={}
        )
        assert not result.is_error
        page = json.loads(result.output)
        assert page["kind"] == "offset"
        assert page["offset"] == 0
        assert page["length"] == 3

    @pytest.mark.asyncio
    async def test_offset_limit_combo(self, system_toolset) -> None:
        for i in range(5):
            body = _llm().model_dump(mode="json")
            body["id"] = f"row-{i}"
            await system_toolset.call(
                tool_name="create_llm_provider", arguments={"entity": body}
            )
        result = await system_toolset.call(
            tool_name="list_llm_providers",
            arguments={"limit": 2, "offset": 1},
        )
        page = json.loads(result.output)
        assert page["length"] == 2
        assert page["offset"] == 1

    @pytest.mark.asyncio
    async def test_offset_and_cursor_together_returns_bad_request(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="list_llm_providers",
            arguments={"offset": 0, "cursor": "abc"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"


# ===========================================================================
# ProviderRegistry: _system short-circuit + immutable invalidation
# ===========================================================================


class TestProviderRegistrySystemHandling:
    @pytest.mark.asyncio
    async def test_get_toolset_resolves_reserved_id(
        self, system_toolset, pr
    ) -> None:
        provider = await pr.get_toolset(SYSTEM_TOOLSET_ID)
        assert provider is system_toolset

    @pytest.mark.asyncio
    async def test_invalidate_system_is_noop(self, system_toolset, pr) -> None:
        await pr.invalidate_toolset(SYSTEM_TOOLSET_ID)
        provider = await pr.get_toolset(SYSTEM_TOOLSET_ID)
        assert provider is system_toolset


# ===========================================================================
# Per-entity smoke — every CRUD set actually dispatches to storage
# (covers the closures generated for embedding/cross_encoder/toolset/agent/
# graph/collection/document/vector_store_config/agent_thread/graph_thread)
# ===========================================================================


def _ce() -> dict:
    from matrix.model.provider import (
        CrossEncoderModel,
        CrossEncoderProvider,
        CrossEncoderProviderType,
        HuggingFaceCrossEncoderConfig,
    )

    return CrossEncoderProvider(
        id="ce-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(token=None),
        limits=Limits(max_concurrency=2),
    ).model_dump(mode="json")


def _toolset_body() -> dict:
    from matrix.model.provider import (
        McpConfig,
        StdioConfig,
        Toolset,
        ToolsetProviderType,
        TransportType,
    )

    return Toolset(
        id="ts-1",
        provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["echo"]),
        ),
    ).model_dump(mode="json")


def _vsc() -> dict:
    from matrix.api.registries.vector_store_registry import (
        ACTIVE_VECTOR_STORE_CONFIG_ID,
    )
    from matrix.model.vector import VectorStoreConfig

    return VectorStoreConfig(
        id=ACTIVE_VECTOR_STORE_CONFIG_ID,
        backend="pgvector",
        settings={"dsn": "x"},
    ).model_dump(mode="json")


def _graph_thread() -> dict:
    now = datetime.now(timezone.utc)
    from matrix.model.graph import GraphThread

    return GraphThread(
        id="gth-1",
        graph_id="gph-1",
        title="hello",
        created_at=now,
        last_activity_at=now,
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "create_tool,delete_tool,body_factory",
    [
        ("create_embedding_provider", "delete_embedding_provider",
         lambda: _emb().model_dump(mode="json")),
        ("create_cross_encoder_provider", "delete_cross_encoder_provider", _ce),
        ("create_toolset", "delete_toolset", _toolset_body),
        ("create_agent", "delete_agent",
         lambda: _agent().model_dump(mode="json")),
        ("create_collection", "delete_collection",
         lambda: _collection().model_dump(mode="json")),
        ("create_vector_store_config", "delete_vector_store_config", _vsc),
        ("create_graph_thread", "delete_graph_thread", _graph_thread),
    ],
)
@pytest.mark.asyncio
async def test_crud_smoke_per_entity(
    system_toolset, create_tool, delete_tool, body_factory
) -> None:
    body = body_factory()
    eid = body["id"]
    create = await system_toolset.call(
        tool_name=create_tool, arguments={"entity": body}
    )
    assert not create.is_error, create.output
    delete = await system_toolset.call(
        tool_name=delete_tool, arguments={"id": eid}
    )
    assert not delete.is_error, delete.output


# ===========================================================================
# find with predicate dict
# ===========================================================================


class TestFindPredicate:
    @pytest.mark.asyncio
    async def test_find_with_predicate_eq(self, system_toolset) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        result = await system_toolset.call(
            tool_name="find_llm_providers",
            arguments={
                "predicate": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "id"},
                    "op": "=",
                    "right": {"kind": "value", "value": "anthropic-1"},
                }
            },
        )
        assert not result.is_error, result.output

    @pytest.mark.asyncio
    async def test_invalid_predicate_returns_validation_error(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="find_llm_providers",
            arguments={"predicate": {"not": "a predicate"}},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"


# ===========================================================================
# Find by meta + cursor pagination + order_by parsing
# ===========================================================================


class TestExtras:
    @pytest.mark.asyncio
    async def test_find_collection_documents_by_meta(self, system_toolset) -> None:
        await system_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        await system_toolset.call(
            tool_name="put_document",
            arguments={
                "id": "doc-1",
                "collection_id": "kb-1",
                "name": "x.txt",
                "content": "x",
                "meta": {"author": "alice"},
            },
        )
        result = await system_toolset.call(
            tool_name="find_collection_documents_by_meta",
            arguments={
                "collection_id": "kb-1",
                "meta_filter": {"author": "alice"},
            },
        )
        assert not result.is_error, result.output

    @pytest.mark.asyncio
    async def test_find_by_meta_collection_404(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="find_collection_documents_by_meta",
            arguments={"collection_id": "missing", "meta_filter": {}},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_search_collection_validation_error_on_missing_query(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="search_collection",
            arguments={"collection_id": "kb-1"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_refresh_collection_404(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="refresh_collection",
            arguments={"collection_id": "missing"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_get_document_content_404(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="get_document_content",
            arguments={"document_id": "missing"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_order_by_parses_field_only(self, system_toolset) -> None:
        body = _llm().model_dump(mode="json")
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        result = await system_toolset.call(
            tool_name="list_llm_providers", arguments={"order_by": ["id"]}
        )
        assert not result.is_error, result.output

    @pytest.mark.asyncio
    async def test_order_by_invalid_direction_returns_bad_request(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="list_llm_providers",
            arguments={"order_by": ["id:bogus"]},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, system_toolset) -> None:
        for i in range(3):
            body = _llm().model_dump(mode="json")
            body["id"] = f"row-{i}"
            await system_toolset.call(
                tool_name="create_llm_provider", arguments={"entity": body}
            )
        result = await system_toolset.call(
            tool_name="list_llm_providers",
            arguments={"limit": 2, "cursor": None},
        )
        page = json.loads(result.output)
        # Default (no offset, no cursor) yields offset-mode response.
        # Explicitly request cursor mode.
        result = await system_toolset.call(
            tool_name="list_llm_providers",
            arguments={"limit": 2, "cursor": "0"},
        )
        page = json.loads(result.output)
        assert page["kind"] == "cursor"

    @pytest.mark.asyncio
    async def test_unknown_tool_raises_unsupported(self, system_toolset) -> None:
        from matrix.model.except_ import UnsupportedContentError

        with pytest.raises(UnsupportedContentError):
            await system_toolset.call(
                tool_name="this_tool_does_not_exist", arguments={}
            )

    @pytest.mark.asyncio
    async def test_call_tool_unknown_toolset_returns_not_found(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": "no-such-toolset",
                "tool_name": "anything",
                "arguments": {},
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_list_toolset_tools_unknown_toolset_returns_not_found(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="list_toolset_tools",
            arguments={"toolset_id": "no-such-toolset"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_create_missing_entity_arg(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="create_llm_provider", arguments={}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_update_missing_id_arg(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"entity": _llm().model_dump(mode="json")},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_update_unknown_id_returns_not_found(
        self, system_toolset
    ) -> None:
        body = _llm().model_dump(mode="json")
        body["id"] = "missing"
        result = await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"id": "missing", "entity": body},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_delete_unknown_id_returns_not_found(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="delete_llm_provider", arguments={"id": "missing"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"
