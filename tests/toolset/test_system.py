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

import httpx
import pytest
import respx
from pydantic import SecretStr

from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.collection import (
    Collection,
    CollectionEmbedder,
    CollectionSearchConfig,
)
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import (
    AnthropicConfig,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
)
from primer.model.storage import (
    CursorPageResponse,
    OffsetPageResponse,
)
from primer.toolset.system import SYSTEM_TOOLSET_ID, build_system_toolset
from tests._support.agent_check_rows import profile_row
from tests._support.caller import ADMIN_CALLER


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

    async def delete_collection(self, collection_id: str, *, conn=None) -> int:
        gone = [k for k, row in self._rows.items() if row.collection_id == collection_id]
        for key in gone:
            del self._rows[key]
        return len(gone)

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
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

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
        model=AgentModel(profile_id="anthropic-1--claude-sonnet-4-6"),
        temperature=0.0,
        tools=[],
        system_prompt=["you are a test"],
    )


def _collection(*, with_search: bool = False) -> Collection:
    search = None
    if with_search:
        search = CollectionSearchConfig(
            embedder=CollectionEmbedder(
                provider_id="hf-1",
                model="sentence-transformers/all-MiniLM-L6-v2",
            ),
            vector_store_provider_id="ssp-1",
        )
    return Collection(
        id="kb-1",
        description="test collection",
        search=search,
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
        assert "get_toolset_auth_status" in names
        assert "call_tool" in names
        assert "describe_tool" in names
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
    async def test_create_get_update_delete_roundtrip(self, system_toolset) -> None:
        body = dump_for_storage(_llm())

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

        result = await system_toolset.call(tool_name="list_llm_providers", arguments={})
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
        body = dump_for_storage(_llm())
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
        body = dump_for_storage(_llm())
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


class TestAggregatedModelProfileTool:
    """Aggregated is a ModelProfile shape now, not an LLMProvider one
    (01a067c4) -- was TestAggregatedLLMProviderTool, round-tripping
    ``provider="aggregated"`` through create_llm_provider/get_llm_provider.
    That shape no longer exists; this exercises the same "a non-trivial
    discriminated shape survives model_validate untouched through the
    generic system toolset" intent against create_model_profile/
    get_model_profile instead. Member existence/kind/self-reference/
    duplicate checks (the model profile router's on_pre_create) USED to
    be skipped here, on the reading that this toolset's CRUD is generic
    Storage[T] access, an admin-level bypass. Task 01a111d1 (D5 phase
    2b) reversed that: the tool runs the same shared checks as the REST
    route, so the members must exist and be single profiles (they are
    seeded below). The refusals themselves are pinned in
    tests/toolset/test_system_validators.py.
    """

    @pytest.mark.asyncio
    async def test_create_aggregated_model_profile_roundtrips(
        self, system_toolset, sp
    ) -> None:
        from primer.model.model_profile import ModelProfile

        for member_id in ("member-1", "member-2"):
            await sp.get_storage(ModelProfile).create(
                ModelProfile(
                    id=member_id, description="a member", kind="single",
                    provider_id="anthropic-1", model_name="m", context_length=1000,
                )
            )
        body = {
            "id": "agg-tool-1",
            "description": "an aggregated profile",
            "kind": "aggregated",
            "members": ["member-1", "member-2"],
            "strategy": "round_robin",
            "failover_point": "before_first_token",
            "failover_on": "transient_and_config",
        }
        result = await system_toolset.call(
            tool_name="create_model_profile", arguments={"entity": body}
        )
        assert not result.is_error, result.output
        created = json.loads(result.output)
        assert created["id"] == "agg-tool-1"
        assert created["kind"] == "aggregated"
        # The aggregated shape survived model_validate untouched.
        assert created["strategy"] == "round_robin"
        assert created["members"] == body["members"]
        assert created["provider_id"] is None

        # Read back through get_model_profile.
        got = await system_toolset.call(
            tool_name="get_model_profile", arguments={"id": "agg-tool-1"}
        )
        assert not got.is_error
        assert json.loads(got.output)["failover_on"] == "transient_and_config"


# ===========================================================================
# Cascade invalidation
# ===========================================================================


class TestCascadeInvalidation:
    @pytest.mark.asyncio
    async def test_update_invalidates_cached_llm_adapter(
        self, system_toolset, pr
    ) -> None:
        body = dump_for_storage(_llm())
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
    async def test_fetch_llm_provider_models_uses_the_saved_provider_probe(
        self, fake_storage_provider, monkeypatch
    ) -> None:
        # The probe stamps the row with a field-scoped patch_if (audit G2-10): a storage that implements it.
        from primer.api.routers import providers as provider_routes

        sp = fake_storage_provider
        provider_registry = ProviderRegistry(sp)  # type: ignore[arg-type]
        system_toolset = build_system_toolset(
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=provider_registry,
        )
        body = dump_for_storage(_llm())
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        discover = AsyncMock(
            return_value=[{"name": "claude-sonnet-4-6", "context_length": 200000}]
        )
        monkeypatch.setattr(
            provider_routes, "_discover_anthropic_models", discover
        )

        result = await system_toolset.call(
            tool_name="fetch_llm_provider_models",
            arguments={"provider_id": "anthropic-1"},
        )
        assert not result.is_error
        assert json.loads(result.output) == {
            "models": [
                {"name": "claude-sonnet-4-6", "context_length": 200000}
            ]
        }
        discover.assert_awaited_once()
        saved = await sp.get_storage(LLMProvider).get("anthropic-1")
        assert saved.last_probe_at is not None
        assert saved.last_probe_ok is True

    @pytest.mark.asyncio
    async def test_fetch_llm_provider_models_keeps_an_edit_made_during_the_probe(
        self, fake_storage_provider, monkeypatch
    ) -> None:
        """Audit G2-10: the tool reaches the same saved-row probe as the REST route, and its stamp writes only the
        ``last_probe_*`` fields, so an edit saved while the probe runs (a rotated key, new limits) survives it."""
        import asyncio

        from primer.api.routers import providers as provider_routes

        sp = fake_storage_provider
        system_toolset = build_system_toolset(storage_provider=sp, provider_registry=ProviderRegistry(sp))  # type: ignore[arg-type]
        await sp.get_storage(LLMProvider).create(_llm())
        started, release = asyncio.Event(), asyncio.Event()

        async def _held(*_a, **_k):
            started.set()
            await release.wait()
            return [{"name": "claude-sonnet-4-6"}]

        monkeypatch.setattr(provider_routes, "_discover_anthropic_models", _held)
        call = asyncio.create_task(system_toolset.call(tool_name="fetch_llm_provider_models", arguments={"provider_id": "anthropic-1"}))
        await asyncio.wait_for(started.wait(), 5)
        row = await sp.get_storage(LLMProvider).get("anthropic-1")
        await sp.get_storage(LLMProvider).update(row.model_copy(update={
            "config": AnthropicConfig(api_key=SecretStr("sk-ant-ROTATED")), "limits": Limits(max_concurrency=9),
        }))
        release.set()
        result = await asyncio.wait_for(call, 5)

        assert not result.is_error, result.output
        saved = await sp.get_storage(LLMProvider).get("anthropic-1")
        assert saved.config.api_key.get_secret_value() == "sk-ant-ROTATED"
        assert saved.limits.max_concurrency == 9
        assert saved.last_probe_ok is True and saved.last_probe_at is not None

    @pytest.mark.asyncio
    async def test_fetch_llm_provider_models_maps_attribute_error(
        self, sp, monkeypatch
    ) -> None:
        from primer.api.routers import providers as provider_routes

        provider_registry = ProviderRegistry(sp)  # type: ignore[arg-type]
        system_toolset = build_system_toolset(
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=provider_registry,
        )
        await sp.get_storage(LLMProvider).create(_llm())
        monkeypatch.setattr(
            provider_routes,
            "_discover_anthropic_models",
            AsyncMock(side_effect=AttributeError("unexpected provider response")),
        )

        result = await system_toolset.call(
            tool_name="fetch_llm_provider_models",
            arguments={"provider_id": "anthropic-1"},
        )

        assert result.is_error
        assert json.loads(result.output)["type"] == "provider-error"

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
# Provider extras: the Hugging Face Hub model check (primer/common/hf_hub.py)
# ===========================================================================


class TestHuggingFaceCheck:
    """``check_huggingface_models`` answers like ``POST /v1/huggingface/_check_models`` and stores nothing.

    The Hub is mocked with ``respx`` at ``HF_ENDPOINT``: no test here reaches the real Hugging Face Hub.
    """

    HUB = "https://hub.test"

    @pytest.fixture(autouse=True)
    def _hub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ENDPOINT", self.HUB)

    @pytest.mark.asyncio
    async def test_the_tool_is_listed_and_admin_only(self, system_toolset) -> None:
        tools = {t.id: t async for t in system_toolset.list_tools()}

        assert tools["check_huggingface_models"].required_role == "admin"
        assert system_toolset.required_role("check_huggingface_models") == "admin"
        assert tools["check_huggingface_models"].yields is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_tool_answers_like_the_route(self, system_toolset) -> None:
        respx.get(f"{self.HUB}/api/models/BAAI/bge-small-en-v1.5/auth-check").mock(
            return_value=httpx.Response(200)
        )

        result = await system_toolset.call(
            tool_name="check_huggingface_models",
            arguments={"models": ["BAAI/bge-small-en-v1.5"]},
        )

        assert not result.is_error, result.output
        assert json.loads(result.output) == {
            "models": [
                {
                    "name": "BAAI/bge-small-en-v1.5",
                    "ok": True,
                    "status": "ok",
                    "detail": "Can be pulled",
                }
            ]
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_model_the_hub_refuses_is_a_result_not_a_tool_error(
        self, system_toolset
    ) -> None:
        respx.get(f"{self.HUB}/api/models/org/gated/auth-check").mock(
            return_value=httpx.Response(403, headers={"X-Error-Code": "GatedRepo"})
        )

        result = await system_toolset.call(
            tool_name="check_huggingface_models",
            arguments={"models": ["org/gated"], "token": "hf_secret_1234"},
        )

        assert not result.is_error
        [check] = json.loads(result.output)["models"]
        assert (check["ok"], check["status"]) == (False, "gated")
        assert (
            respx.calls.last.request.headers["authorization"]
            == "Bearer hf_secret_1234"
        )
        assert "hf_secret_1234" not in result.output

    @pytest.mark.asyncio
    async def test_an_empty_model_list_is_an_error_result(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="check_huggingface_models", arguments={"models": []}
        )

        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_more_than_fifty_models_is_an_error_result(
        self, system_toolset
    ) -> None:
        result = await system_toolset.call(
            tool_name="check_huggingface_models",
            arguments={"models": [f"org/m{i}" for i in range(51)]},
        )

        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_a_bad_argument_does_not_echo_the_token(self, system_toolset) -> None:
        result = await system_toolset.call(
            tool_name="check_huggingface_models",
            arguments={"models": [], "token": "hf_secret_1234"},
        )

        assert result.is_error
        assert "hf_secret_1234" not in result.output


# ===========================================================================
# Provider extras: the draft tests of embedding and cross-encoder providers
# ===========================================================================


class TestDraftTestTools:
    """``test_embedding_provider`` and ``test_cross_encoder_provider`` answer like ``POST /v1/embedding_providers/_test`` and
    ``POST /v1/cross_encoder_providers/_test`` and store nothing. Every upstream is mocked with ``respx``.
    """

    HUB = "https://hub.test"
    TOOLS = ("test_embedding_provider", "test_cross_encoder_provider")

    @pytest.fixture(autouse=True)
    def _hub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ENDPOINT", self.HUB)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", TOOLS)
    async def test_tools_are_admin_only(self, system_toolset, name: str) -> None:
        tools = {t.id: t async for t in system_toolset.list_tools()}

        assert tools[name].required_role == "admin"
        assert system_toolset.required_role(name) == "admin"
        assert tools[name].yields is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_tools_answer_like_the_routes(self, system_toolset) -> None:
        """The same cores answer the tool and the route; gemini via respx is the arm that needs Google, the Hub is the other."""
        from primer.api.routers.providers import (
            cross_encoder_draft_test,
            embedding_draft_test,
        )
        from primer.llm.gemini import GEMINI_BASE_URL

        respx.get(f"{GEMINI_BASE_URL}/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-embedding-001",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        )
        respx.get(f"{self.HUB}/api/models/BAAI/bge-reranker-v2-m3/auth-check").mock(
            return_value=httpx.Response(200)
        )
        gemini = {"provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}}
        reranker = {"models": [{"name": "BAAI/bge-reranker-v2-m3"}]}

        embedding = await system_toolset.call(
            tool_name="test_embedding_provider", arguments=gemini
        )
        cross_encoder = await system_toolset.call(
            tool_name="test_cross_encoder_provider", arguments=reranker
        )

        assert not embedding.is_error, embedding.output
        assert json.loads(embedding.output) == await embedding_draft_test(gemini)
        assert json.loads(embedding.output) == {
            "ok": True,
            "models": ["gemini-embedding-001"],
        }
        assert not cross_encoder.is_error, cross_encoder.output
        assert json.loads(cross_encoder.output) == await cross_encoder_draft_test(
            reranker
        )
        assert json.loads(cross_encoder.output)["ok"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_the_hub_check_of_an_embedding_draft_is_an_answer(
        self, system_toolset
    ) -> None:
        respx.get(f"{self.HUB}/api/models/org/gated/auth-check").mock(
            return_value=httpx.Response(403, headers={"X-Error-Code": "GatedRepo"})
        )

        result = await system_toolset.call(
            tool_name="test_embedding_provider",
            arguments={
                "provider": "huggingface",
                "config": {"token": "hf_secret_1234"},
                "models": [{"name": "org/gated"}],
            },
        )

        assert not result.is_error, "a model the Hub refuses is an answer, not a tool error"
        body = json.loads(result.output)
        assert body["ok"] is False and body["models"] == []
        assert "1 of 1 models failed the Hub check: org/gated (gated)" in body["error"]
        assert "hf_secret_1234" not in result.output

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", TOOLS)
    async def test_a_draft_that_is_not_a_row_is_an_answer_not_a_tool_error(
        self, system_toolset, name: str
    ) -> None:
        result = await system_toolset.call(tool_name=name, arguments={})

        assert not result.is_error
        body = json.loads(result.output)
        assert body["ok"] is False and body["error"].startswith("invalid draft")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", TOOLS)
    @pytest.mark.parametrize(
        "arguments",
        [{"models": "BAAI/bge-small-en-v1.5"}, {"config": "nope"}, {"provider": 5}],
    )
    async def test_arguments_of_the_wrong_shape_are_a_validation_error(
        self, system_toolset, name: str, arguments: dict
    ) -> None:
        result = await system_toolset.call(tool_name=name, arguments=arguments)

        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    @respx.mock
    async def test_id_and_limits_in_the_arguments_are_ignored_and_nothing_is_stored(
        self, system_toolset, sp
    ) -> None:
        respx.get(f"{self.HUB}/api/models/BAAI/bge-small-en-v1.5/auth-check").mock(
            return_value=httpx.Response(200)
        )

        result = await system_toolset.call(
            tool_name="test_embedding_provider",
            arguments={
                "id": "ignored",
                "limits": "ignored",
                "provider": "huggingface",
                "models": [{"name": "BAAI/bge-small-en-v1.5"}],
            },
        )

        assert json.loads(result.output)["ok"] is True
        assert await sp.get_storage(EmbeddingProvider).get("ignored") is None
        assert sp.get_storage(EmbeddingProvider)._data == {}


# ===========================================================================
# Toolset extras: list_toolset_tools, call_tool
# ===========================================================================


class TestToolsetExtras:
    @pytest.mark.asyncio
    async def test_list_toolset_tools_can_introspect_self(self, system_toolset) -> None:
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
        body = dump_for_storage(_llm())
        await system_toolset.call(
            tool_name="create_llm_provider", arguments={"entity": body}
        )
        arguments = {
            "toolset_id": SYSTEM_TOOLSET_ID,
            "tool_name": "get_llm_provider",
            "arguments": {"id": "anthropic-1"},
        }
        result = await system_toolset.call(
            tool_name="call_tool", arguments=arguments, ctx=_ctx(),
        )
        assert not result.is_error
        assert json.loads(result.output)["id"] == "anthropic-1"
        # The same call with no context ranks as an ordinary user: the admin inner tool is refused (pivot P1).
        refused = await system_toolset.call(tool_name="call_tool", arguments=arguments)
        assert json.loads(refused.output)["type"] == "forbidden"

    @pytest.mark.asyncio
    async def test_call_tool_propagates_inner_error(self, system_toolset) -> None:
        arguments = {
            "toolset_id": SYSTEM_TOOLSET_ID,
            "tool_name": "get_llm_provider",
            "arguments": {"id": "missing"},
        }
        result = await system_toolset.call(
            tool_name="call_tool", arguments=arguments, ctx=_ctx(),
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"
        # With no context the role check refuses it before the inner tool runs (pivot P1).
        refused = await system_toolset.call(tool_name="call_tool", arguments=arguments)
        assert json.loads(refused.output)["type"] == "forbidden"


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
                "slug": "hello.txt",
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
                "slug": "hello.txt",
                "path": "hello.txt",
                "content": "this is the content",
                "title": "Hello",
            },
        )
        assert not result.is_error, result.output
        result = await system_toolset.call(
            tool_name="get_document_content",
            arguments={
                "collection_id": "kb-1",
                "slug": "hello.txt",
                "path": "hello.txt",
            },
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
                    "slug": "x.txt",
                    "path": "x.txt",
                    "content": content,
                },
            )
            assert not result.is_error, result.output
        result = await system_toolset.call(
            tool_name="get_document_content",
            arguments={"collection_id": "kb-1", "slug": "x.txt", "path": "x.txt"},
        )
        assert json.loads(result.output)["content"] == "second"


# ===========================================================================
# Pagination contract
# ===========================================================================


class TestPagination:
    @pytest.mark.asyncio
    async def test_default_pagination(self, system_toolset) -> None:
        for i in range(3):
            body = dump_for_storage(_llm())
            body["id"] = f"row-{i}"
            await system_toolset.call(
                tool_name="create_llm_provider", arguments={"entity": body}
            )
        result = await system_toolset.call(tool_name="list_llm_providers", arguments={})
        assert not result.is_error
        page = json.loads(result.output)
        assert page["kind"] == "offset"
        assert page["offset"] == 0
        assert page["length"] == 3

    @pytest.mark.asyncio
    async def test_offset_limit_combo(self, system_toolset) -> None:
        for i in range(5):
            body = dump_for_storage(_llm())
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
    async def test_get_toolset_resolves_reserved_id(self, system_toolset, pr) -> None:
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
# graph/collection/document/vector_store_config/graph_thread)
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
        (
            "create_embedding_provider",
            "delete_embedding_provider",
            lambda: dump_for_storage(_emb()),
        ),
        ("create_cross_encoder_provider", "delete_cross_encoder_provider", _ce),
        ("create_toolset", "delete_toolset", _toolset_body),
        ("create_agent", "delete_agent", lambda: _agent().model_dump(mode="json")),
        (
            "create_collection",
            "delete_collection",
            lambda: _collection().model_dump(mode="json"),
        ),
        ("create_graph_thread", "delete_graph_thread", _graph_thread),
    ],
)
@pytest.mark.asyncio
async def test_crud_smoke_per_entity(
    system_toolset, sp, create_tool, delete_tool, body_factory
) -> None:
    body = body_factory()
    eid = body["id"]
    if create_tool == "create_agent":
        # An agent must name a stored profile: the tool refuses a dangling one, as the REST route does (A-09).
        profile = profile_row(body["model"]["profile_id"])
        await sp.get_storage(type(profile)).create(profile)
    # An admin writes the rows: a stdio MCP toolset (``_toolset_body``) is refused to anyone else, and to a call with no identity.
    create = await system_toolset.call(
        tool_name=create_tool, arguments={"entity": body}, ctx=ADMIN_CALLER
    )
    assert not create.is_error, create.output
    delete = await system_toolset.call(tool_name=delete_tool, arguments={"id": eid}, ctx=ADMIN_CALLER)
    assert not delete.is_error, delete.output


# ===========================================================================
# find with predicate dict
# ===========================================================================


class TestFindPredicate:
    @pytest.mark.asyncio
    async def test_find_with_predicate_eq(self, system_toolset) -> None:
        body = dump_for_storage(_llm())
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
                "slug": "x.txt",
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
            arguments={
                "collection_id": "kb-1",
                "slug": "missing.md",
                "path": "missing.md",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_order_by_parses_field_only(self, system_toolset) -> None:
        body = dump_for_storage(_llm())
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
            body = dump_for_storage(_llm())
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
            arguments={"entity": dump_for_storage(_llm())},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_update_unknown_id_returns_not_found(self, system_toolset) -> None:
        body = dump_for_storage(_llm())
        body["id"] = "missing"
        result = await system_toolset.call(
            tool_name="update_llm_provider",
            arguments={"id": "missing", "entity": body},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_delete_unknown_id_returns_not_found(self, system_toolset) -> None:
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
            arguments={"entity": dump_for_storage(_emb())},
        )
        await wired_toolset.call(
            tool_name="create_collection",
            arguments={"entity": _collection(with_search=True).model_dump(mode="json")},
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
    async def test_unknown_collection_returns_not_found(self, wired_toolset) -> None:
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

    # An admin's run: call_tool checks the INNER tool's role against the run's principal (pivot P1), and these tests run admin tools
    # (get_llm_provider) through it. A context with no principal ranks as an ordinary user and is refused them.
    return ToolContext(
        tool_call_id=tool_call_id,
        session_id=session_id,
        workspace_id="ws-1",
        chat_id=chat_id,
        initiated_by=ADMIN_CALLER.initiated_by,
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
            arguments={"entity": dump_for_storage(_llm())},
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
            arguments={"entity": dump_for_storage(_llm())},
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
        # The gated inner tool is a USER-role one: a call with no context
        # ranks as an ordinary user, so an admin tool would be refused by
        # the role check before the gate is reached (pivot P1).
        from primer.model.tool_approval import ToolApprovalPolicy

        await sp.get_storage(ToolApprovalPolicy).create(
            _required_policy(SYSTEM_TOOLSET_ID, "list_toolset_tools")
        )
        result = await system_toolset.call(
            tool_name="call_tool",
            arguments={
                "toolset_id": SYSTEM_TOOLSET_ID,
                "tool_name": "list_toolset_tools",
                "arguments": {"toolset_id": SYSTEM_TOOLSET_ID},
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "approval-required"


# ===========================================================================
# 01a06610: the meta-dispatch gate shares the app-level resolver's cache
# ===========================================================================


class TestCallToolResolverSharing:
    @pytest.mark.asyncio
    async def test_injected_resolver_invalidate_reaches_meta_dispatch(
        self, sp: _SP, pr: ProviderRegistry
    ) -> None:
        """The operator ``/invalidate`` clears ONE shared cache: after
        call_tool caches policy=None for an inner tool, creating a policy
        row and invalidating the injected (app-level) resolver re-gates
        the very next dispatch, instead of a private second resolver
        serving the stale verdict until its TTL runs out."""
        from primer.agent.approval import ApprovalResolver
        from primer.model.tool_approval import ToolApprovalPolicy
        from primer.model.yield_ import YieldToWorker

        resolver = ApprovalResolver(
            storage=sp.get_storage(ToolApprovalPolicy),
            cache_ttl_seconds=600.0,  # a TTL expiry can't mask the miss
        )
        provider = build_system_toolset(
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=pr,
            approval_resolver=resolver,
        )
        pr._system_toolset_provider = provider  # type: ignore[attr-defined]
        assert provider.approval_resolver is resolver

        await provider.call(
            tool_name="create_llm_provider",
            arguments={"entity": dump_for_storage(_llm())},
        )
        call_args = {
            "toolset_id": SYSTEM_TOOLSET_ID,
            "tool_name": "get_llm_provider",
            "arguments": {"id": "anthropic-1"},
        }
        # First dispatch caches policy=None for the inner tool's key.
        result = await provider.call(
            tool_name="call_tool", arguments=call_args, ctx=_ctx(),
        )
        assert not result.is_error, result.output

        await sp.get_storage(ToolApprovalPolicy).create(
            _required_policy(SYSTEM_TOOLSET_ID, "get_llm_provider")
        )
        # Still cached: the new row is invisible until an invalidate.
        result = await provider.call(
            tool_name="call_tool", arguments=call_args, ctx=_ctx(),
        )
        assert not result.is_error, result.output

        # What POST /tool_approval_policies/invalidate does to the
        # app-level instance -- must re-gate meta-dispatch immediately.
        resolver.invalidate()
        with pytest.raises(YieldToWorker):
            await provider.call(
                tool_name="call_tool", arguments=call_args, ctx=_ctx(),
            )
