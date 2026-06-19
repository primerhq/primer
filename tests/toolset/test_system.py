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

from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
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
from primer.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
)
from primer.model.thread import Thread
from primer.toolset.system import SYSTEM_TOOLSET_ID, build_system_toolset


# ===========================================================================
# In-memory fakes
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str, *, conn: Any | None = None) -> Any | None:
        return self._data.get(id)

    async def create(self, e: Any, *, conn: Any | None = None) -> Any:
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e: Any, *, conn: Any | None = None) -> Any:
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id: str, *, conn: Any | None = None) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        from primer.model.storage import OffsetPage

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


class _ContentStore:
    """Minimal in-memory DocumentContentStore for the path-addressed tools.

    Keyed by document_id; enforces the (collection_id, path) uniqueness the
    real backends do so conflict/move semantics match.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Any] = {}  # document_id -> ContentRow

    async def ensure_schema(self) -> None:
        return

    def _find_by_path(self, collection_id: str, path: str):
        for row in self._rows.values():
            if row.collection_id == collection_id and row.path == path:
                return row
        return None

    async def get(self, document_id: str, *, conn=None):
        row = self._rows.get(document_id)
        return row.content if row is not None else None

    async def get_by_path(self, collection_id: str, path: str, *, conn=None):
        return self._find_by_path(collection_id, path)

    async def resolve_id(self, collection_id: str, path: str, *, conn=None):
        row = self._find_by_path(collection_id, path)
        return row.document_id if row is not None else None

    async def upsert(self, *, document_id, collection_id, path, content, conn=None):
        from primer.int.document_content import ContentRow

        clash = self._find_by_path(collection_id, path)
        if clash is not None and clash.document_id != document_id:
            raise ConflictError(f"path {path!r} already taken in {collection_id!r}")
        self._rows[document_id] = ContentRow(
            document_id=document_id,
            collection_id=collection_id,
            path=path,
            content=content,
        )

    async def delete(self, document_id: str, *, conn=None):
        self._rows.pop(document_id, None)

    async def move(self, document_id: str, new_path: str, *, conn=None):
        row = self._rows.get(document_id)
        if row is None:
            raise NotFoundError(f"no content row for {document_id!r}")
        clash = self._find_by_path(row.collection_id, new_path)
        if clash is not None and clash.document_id != document_id:
            raise ConflictError(f"path {new_path!r} already taken")
        self._rows[document_id] = row.model_copy(update={"path": new_path})

    async def list(self, collection_id: str, *, prefix: str | None = None):
        from primer.int.document_content import ContentListEntry

        out = []
        for row in self._rows.values():
            if row.collection_id != collection_id:
                continue
            if prefix is not None and not row.path.startswith(prefix):
                continue
            out.append(
                ContentListEntry(
                    document_id=row.document_id,
                    path=row.path,
                    size=len(row.content),
                )
            )
        return out


class _NullTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}
        self._content_store = _ContentStore()

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())

    def get_content_store(self) -> _ContentStore:
        return self._content_store

    def transaction(self) -> _NullTxn:
        return _NullTxn()

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
def system_toolset(sp: _SP, pr: ProviderRegistry):
    provider = build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
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
        search_provider_id="ssp-test",
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


# The (entity_label, entity_label_plural) pairs the CRUD factory generates a
# six-verb tool set for. Mirrors ``crud_specs`` in build_system_toolset.
_CRUD_ENTITIES = [
    ("llm_provider", "llm_providers"),
    ("embedding_provider", "embedding_providers"),
    ("cross_encoder_provider", "cross_encoder_providers"),
    ("toolset", "toolsets"),
    ("agent", "agents"),
    ("graph", "graphs"),
    ("collection", "collections"),
    ("document", "documents"),
    ("agent_thread", "agent_threads"),
    ("graph_thread", "graph_threads"),
    ("semantic_search_provider", "semantic_search_providers"),
    ("tool_approval_policy", "tool_approval_policies"),
    ("channel_provider", "channel_providers"),
    ("channel", "channels"),
]


# ===========================================================================
# Catalog tests
# ===========================================================================


class TestCatalog:
    @pytest.mark.asyncio
    async def test_toolset_id_is_reserved(self, system_toolset) -> None:
        assert SYSTEM_TOOLSET_ID == "system"
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
            ("agent_thread", "agent_threads"),
            ("graph_thread", "graph_threads"),
            ("semantic_search_provider", "semantic_search_providers"),
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
        assert "list_documents" in names
        assert "move_document" in names
        assert "invalidate_semantic_search_provider" in names

    @pytest.mark.asyncio
    async def test_each_tool_has_non_empty_description(self, system_toolset) -> None:
        from tests.toolset._desc_conformance import assert_tool_conforms
        async for t in system_toolset.list_tools():
            assert_tool_conforms(t)
            assert isinstance(t.args_schema, dict)

    @pytest.mark.asyncio
    async def test_crud_tools_conform(self, system_toolset) -> None:
        from tests.toolset._desc_conformance import assert_tool_conforms

        # The CRUD factory emits exactly these six verbs per entity; the
        # non-migrated extras (list_toolset_tools, find_collection_documents_
        # by_meta, ...) are out of scope for this task (Task 9) so we match
        # the generated tools by their exact verb-prefixed ids.
        crud_ids = set()
        for entity, plural in _CRUD_ENTITIES:
            crud_ids.update(
                {
                    f"list_{plural}",
                    f"get_{entity}",
                    f"create_{entity}",
                    f"update_{entity}",
                    f"delete_{entity}",
                    f"find_{plural}",
                }
            )
        async for t in system_toolset.list_tools():
            if t.id in crud_ids:
                assert_tool_conforms(t)

    @pytest.mark.asyncio
    async def test_all_system_tools_conform(self, system_toolset) -> None:
        from tests.toolset._desc_conformance import assert_tool_conforms

        async for t in system_toolset.list_tools():
            assert_tool_conforms(t)


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
                "collection_id": "kb-1",
                "path": "hello.txt",
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
    async def test_search_collection_unavailable_without_registry(
        self, system_toolset
    ) -> None:
        # The default fixture wires no SemanticSearchRegistry, so search
        # degrades to ``unavailable`` (no longer the old not-implemented
        # stub). The wired path is covered in TestSearchCollectionWired.
        result = await system_toolset.call(
            tool_name="search_collection",
            arguments={"collection_id": "kb-1", "query": "anything", "top_k": 5},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "unavailable"

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
                "collection_id": "kb-1",
                "path": "hello.txt",
                "content": "this is the content",
                "title": "Hello",
            },
        )
        assert not result.is_error, result.output
        result = await system_toolset.call(
            tool_name="get_document_content",
            arguments={"collection_id": "kb-1", "path": "hello.txt"},
        )
        assert not result.is_error
        body = json.loads(result.output)
        assert body["content"] == "this is the content"
        assert body["path"] == "hello.txt"
        assert body["title"] == "Hello"

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
                    "collection_id": "kb-1",
                    "path": "x.txt",
                    "content": content,
                },
            )
            assert not result.is_error, result.output
        result = await system_toolset.call(
            tool_name="get_document_content",
            arguments={"collection_id": "kb-1", "path": "x.txt"},
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
# Per-entity smoke - every CRUD set actually dispatches to storage
# (covers the closures generated for embedding/cross_encoder/toolset/agent/
# graph/collection/document/vector_store_config/agent_thread/graph_thread)
# ===========================================================================


def _ce() -> dict:
    from primer.model.provider import (
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
    from primer.model.provider import (
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


def _graph_thread() -> dict:
    now = datetime.now(timezone.utc)
    from primer.model.graph import GraphThread

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
                "collection_id": "kb-1",
                "path": "x.txt",
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
            arguments={"collection_id": "kb-1", "path": "missing.md"},
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
        from primer.model.except_ import UnsupportedContentError

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


# ===========================================================================
# FIX B: search_collection wired to the SearchService (embedder + store)
# ===========================================================================


class _FakeEmbedder:
    """Embedder stub: returns a fixed vector regardless of input."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, *, model, inputs, output_dimensions=None, config=None):
        from primer.model.embedding import EmbedResponse, Embedding

        return EmbedResponse(
            model=model,
            embeddings=[Embedding(index=0, vector=self._vector)],
        )


class _FakeVectorStore:
    """In-memory vector store returning preset ranked hits."""

    def __init__(self, results) -> None:
        self._results = results
        self.calls: list[tuple] = []

    async def search(self, collection_id, vector, k):
        self.calls.append((collection_id, list(vector), k))
        return self._results[:k]


class _FakeSSR:
    """SemanticSearchRegistry duck-type exposing get_store."""

    def __init__(self, store) -> None:
        self._store = store

    async def get_store(self, ssp_id):
        return self._store


def _ranked_hits():
    from primer.model.vector import EmbeddingRecord, SearchResult

    return [
        SearchResult(
            record=EmbeddingRecord(
                collection_id="kb-1",
                document_id="doc-1",
                chunk_id="c0",
                text="onboarding starts on day one",
                vector=[0.1, 0.2, 0.3, 0.4],
                meta={"source": "handbook"},
            ),
            score=0.91,
        ),
        SearchResult(
            record=EmbeddingRecord(
                collection_id="kb-1",
                document_id="doc-2",
                chunk_id="c0",
                text="benefits enrolment window",
                vector=[0.5, 0.6, 0.7, 0.8],
                meta={"source": "hr"},
            ),
            score=0.42,
        ),
    ]


class TestSearchCollectionWired:
    @pytest.fixture
    def store(self):
        return _FakeVectorStore(_ranked_hits())

    @pytest.fixture
    def wired_toolset(self, sp: _SP, store: _FakeVectorStore):
        # Real ProviderRegistry but with an embedder factory that yields
        # the _FakeEmbedder so get_embedder returns something with .embed.
        registry = ProviderRegistry(
            sp,  # type: ignore[arg-type]
            llm_factory=lambda p: object(),
            embedder_factory=lambda p: _FakeEmbedder([0.1, 0.2, 0.3, 0.4]),
            cross_encoder_factory=lambda p: object(),
            toolset_factory=lambda t: object(),
        )
        provider = build_system_toolset(
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=registry,
            semantic_search_registry=_FakeSSR(store),  # type: ignore[arg-type]
        )
        registry._system_toolset_provider = provider  # type: ignore[attr-defined]
        return provider

    @pytest.mark.asyncio
    async def test_returns_ranked_hits(self, wired_toolset, store) -> None:
        # Seed the embedding provider + collection so the handler can
        # resolve the collection's embedder and search_provider_id.
        await wired_toolset.call(
            tool_name="create_embedding_provider",
            arguments={"entity": _emb().model_dump(mode="json")},
        )
        await wired_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection().model_dump(mode="json")},
        )
        result = await wired_toolset.call(
            tool_name="search_collection",
            arguments={"collection_id": "kb-1", "query": "onboarding", "top_k": 5},
        )
        assert not result.is_error, result.output
        body = json.loads(result.output)
        # No longer the not-implemented sentinel.
        assert "type" not in body
        hits = body["hits"]
        assert [h["document_id"] for h in hits] == ["doc-1", "doc-2"]
        assert hits[0]["score"] == 0.91
        assert hits[0]["text"] == "onboarding starts on day one"
        assert hits[0]["chunk_id"] == "c0"
        assert hits[0]["meta"] == {"source": "handbook"}
        # The store was searched, scoped to the collection.
        assert store.calls and store.calls[0][0] == "kb-1"

    @pytest.mark.asyncio
    async def test_unknown_collection_returns_not_found(
        self, wired_toolset
    ) -> None:
        result = await wired_toolset.call(
            tool_name="search_collection",
            arguments={"collection_id": "missing", "query": "x"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"


# ===========================================================================
# FIX A: call_tool enforces the approval gate (parks instead of bypassing)
# ===========================================================================


def _required_policy(toolset_id: str, tool_name: str):
    from primer.model.tool_approval import (
        RequiredApprovalConfig,
        ToolApprovalPolicy,
    )

    return ToolApprovalPolicy(
        id="tap-1",
        toolset_id=toolset_id,
        tool_name=tool_name,
        enabled=True,
        approval=RequiredApprovalConfig(),
    )


def _ctx(tool_call_id="call-1", session_id="sess-1", chat_id=None):
    from primer.model.yield_ import ToolContext

    return ToolContext(
        tool_call_id=tool_call_id,
        session_id=session_id,
        workspace_id="ws-1",
        chat_id=chat_id,
    )


class TestCallToolApprovalGate:
    @pytest.mark.asyncio
    async def test_gated_inner_tool_yields_for_approval(
        self, system_toolset, sp: _SP
    ) -> None:
        from primer.model.tool_approval import ToolApprovalPolicy
        from primer.model.yield_ import YieldToWorker

        # Configure an approval policy for the INNER tool call_tool targets.
        await sp.get_storage(ToolApprovalPolicy).create(
            _required_policy(SYSTEM_TOOLSET_ID, "get_llm_provider")
        )
        # Seed the inner tool's data so a bypass would actually succeed
        # (proves the yield is the gate, not a missing row).
        await system_toolset.call(
            tool_name="create_llm_provider",
            arguments={"entity": _llm().model_dump(mode="json")},
        )

        with pytest.raises(YieldToWorker) as exc_info:
            await system_toolset.call(
                tool_name="call_tool",
                arguments={
                    "toolset_id": SYSTEM_TOOLSET_ID,
                    "tool_name": "get_llm_provider",
                    "arguments": {"id": "anthropic-1"},
                },
                ctx=_ctx(),
            )
        yielded = exc_info.value.yielded
        assert yielded.tool_name == "_approval"
        assert yielded.event_key == "tool_approval:sess-1:call-1"
        meta = yielded.resume_metadata
        # Resume re-dispatches the inner tool via its owning toolset.
        assert meta["via_call_tool"]["toolset_id"] == SYSTEM_TOOLSET_ID
        assert meta["original_call"]["name"] == "get_llm_provider"
        assert meta["original_call"]["arguments"] == {"id": "anthropic-1"}

    @pytest.mark.asyncio
    async def test_non_gated_inner_tool_dispatches_normally(
        self, system_toolset
    ) -> None:
        # No policy stored -> call_tool dispatches the inner tool unchanged.
        await system_toolset.call(
            tool_name="create_llm_provider",
            arguments={"entity": _llm().model_dump(mode="json")},
        )
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": SYSTEM_TOOLSET_ID,
                "tool_name": "get_llm_provider",
                "arguments": {"id": "anthropic-1"},
            },
            ctx=_ctx(),
        )
        assert not result.is_error, result.output
        assert json.loads(result.output)["id"] == "anthropic-1"

    @pytest.mark.asyncio
    async def test_gated_tool_without_park_surface_fails_closed(
        self, system_toolset, sp: _SP
    ) -> None:
        # No session/chat to park onto (ctx is None) -> the gate must NOT
        # be bypassed; the call fails closed with approval-required.
        from primer.model.tool_approval import ToolApprovalPolicy

        await sp.get_storage(ToolApprovalPolicy).create(
            _required_policy(SYSTEM_TOOLSET_ID, "get_llm_provider")
        )
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": SYSTEM_TOOLSET_ID,
                "tool_name": "get_llm_provider",
                "arguments": {"id": "anthropic-1"},
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "approval-required"
