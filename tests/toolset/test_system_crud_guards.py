"""The system CRUD tools carry the guards the REST routers carry (task 01a111d1, D5 phase 1: managed, reserved, references, cache).

``make_crud_router`` gives every REST router declarative guards; the system toolset's generic ``create_/update_/delete_<entity>``
tools re-implemented the six verbs with none of them (the audit behind this file, reproduced on a real sqlite store through the
real toolset). Through a tool an agent could create, edit and delete harness-managed rows, create and delete the reserved
bootstrap providers, and delete a model profile that agents still use. This pins the same behaviour as REST for each:

* **managed rows** (agent, graph, collection, model_profile, toolset): a body that sets ``harness_id`` is refused on create; an update or a
  delete of a row that has one is refused (REST 422 / 409 / 409), and so is SETTING it on an unmanaged row (stricter than REST,
  which only looks at the stored row; a tool must not claim a row for a harness);
* **reserved ids**: creating a reserved id is a conflict, deleting one is forbidden even when no row exists (REST 409 / 403),
  updating one stays allowed (REST allows it): embedding ``huggingface``, cross-encoder ``huggingface-ce``, semantic search
  ``lance``, the toolset scopes ``external`` / ``workspace`` / ``workspace_ext`` (create only), the default artifact provider
  (delete only);
* **reference blocks** on delete: a model profile an agent uses or another profile lists as a member, a channel provider a
  channel uses, a toolset a tool-approval policy names (REST 409 ``in_use_by``);
* **cache**: updating or deleting a model profile drops its cached aggregated LLM, as REST does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent
from primer.model.channel import Channel, ChannelProvider, ChannelProviderType, SlackChannelProviderConfig
from primer.model.collection import Collection
from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from primer.model.provider import (
    ArtifactStorageProvider,
    EmbeddingProvider,
    LLMProvider,
    SqliteConfig,
    Toolset,
)
from primer.model.tool_approval import ToolApprovalPolicy
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset
from tests._support.caller import ADMIN_CALLER
from tests.api.test_semantic_search_registry import _make_row as _ssp_row
from tests.toolset.test_system import _agent, _ce, _collection, _emb, _llm, _required_policy, _toolset_body


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    # The provider the test profiles name (``_profile()`` defaults to anthropic-1): since task 01a111d1 D5 phase 2b the tools refuse a
    # single profile whose provider does not exist, as the REST route does.
    await sp.get_storage(LLMProvider).create(_llm())
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    invalidated: list[str] = []

    async def spy(profile_id: str) -> None:
        invalidated.append(profile_id)

    registry.invalidate_aggregated_llm = spy  # type: ignore[method-assign]
    toolset = build_system_toolset(storage_provider=sp, provider_registry=registry)
    registry._system_toolset_provider = toolset
    yield sp, toolset, invalidated
    await sp.aclose()


async def _call(toolset, name: str, **args):
    # As an admin: the guards under test are what stands between an ALLOWED writer and a row, and a stdio toolset body (the
    # fixture for ``toolset``) is refused to every other caller before any guard runs (tests/toolset/test_system_toolset_stdio_admin.py).
    result = await toolset.call(tool_name=name, arguments=args, ctx=ADMIN_CALLER)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


def _profile(profile_id: str = "mp-1", **fields) -> ModelProfile:
    base = dict(id=profile_id, description="a profile", kind="single", provider_id="anthropic-1", model_name="m", context_length=1000)
    base.update(fields)
    return ModelProfile(**base)


# kind -> (model class, a valid body for it, the id inside the body)
MANAGED_KINDS = {
    "agent": (Agent, lambda: _agent().model_dump(mode="json"), "agt-1"),
    "graph": (Graph, lambda: Graph(id="gr-1", description="a draft graph").model_dump(mode="json"), "gr-1"),
    "collection": (Collection, lambda: _collection().model_dump(mode="json"), "kb-1"),
    "model_profile": (ModelProfile, lambda: _profile().model_dump(mode="json"), "mp-1"),
    "toolset": (Toolset, _toolset_body, "ts-1"),
}


@pytest.mark.parametrize("kind", sorted(MANAGED_KINDS))
class TestHarnessManagedRowsAreNotWritableThroughATool:
    @pytest.mark.asyncio
    async def test_a_body_that_sets_harness_id_is_refused_on_create(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]

        is_error, answer = await _call(toolset, f"create_{kind}", entity={**body(), "harness_id": "hns_x"})

        assert is_error and answer["type"] == "bad-request"
        assert await sp.get_storage(model).get(entity_id) is None, "a managed row was created through a tool"

    @pytest.mark.asyncio
    async def test_a_managed_row_cannot_be_updated(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]
        await sp.get_storage(model).create(model.model_validate({**body(), "harness_id": "hns_x"}))
        _, served = await _call(toolset, f"get_{kind}", id=entity_id)

        is_error, answer = await _call(toolset, f"update_{kind}", id=entity_id, entity=served)

        assert is_error and answer["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_a_managed_row_cannot_be_released_by_an_update_that_omits_harness_id(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]
        await sp.get_storage(model).create(model.model_validate({**body(), "harness_id": "hns_x"}))
        _, served = await _call(toolset, f"get_{kind}", id=entity_id)
        without_owner = {key: value for key, value in served.items() if key != "harness_id"}

        is_error, answer = await _call(toolset, f"update_{kind}", id=entity_id, entity=without_owner)

        assert is_error and answer["type"] == "conflict"
        assert (await sp.get_storage(model).get(entity_id)).harness_id == "hns_x", "an update released a managed row"

    @pytest.mark.asyncio
    async def test_a_managed_row_cannot_be_deleted(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]
        await sp.get_storage(model).create(model.model_validate({**body(), "harness_id": "hns_x"}))

        is_error, answer = await _call(toolset, f"delete_{kind}", id=entity_id)

        assert is_error and answer["type"] == "conflict"
        assert await sp.get_storage(model).get(entity_id) is not None

    @pytest.mark.asyncio
    async def test_harness_id_cannot_be_set_on_an_unmanaged_row_by_an_update(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]
        await sp.get_storage(model).create(model.model_validate(body()))
        _, served = await _call(toolset, f"get_{kind}", id=entity_id)

        is_error, answer = await _call(toolset, f"update_{kind}", id=entity_id, entity={**served, "harness_id": "hns_x"})

        assert is_error and answer["type"] in ("bad-request", "conflict")
        assert (await sp.get_storage(model).get(entity_id)).harness_id is None

    @pytest.mark.asyncio
    async def test_an_unmanaged_row_is_created_updated_and_deleted_as_before(self, world, kind) -> None:
        sp, toolset, _ = world
        model, body, entity_id = MANAGED_KINDS[kind]

        created_error, _ = await _call(toolset, f"create_{kind}", entity=body())
        _, served = await _call(toolset, f"get_{kind}", id=entity_id)
        updated_error, _ = await _call(toolset, f"update_{kind}", id=entity_id, entity=served)
        deleted_error, _ = await _call(toolset, f"delete_{kind}", id=entity_id)

        assert not (created_error or updated_error or deleted_error)
        assert await sp.get_storage(model).get(entity_id) is None


def _reserved_cases():
    return [
        ("embedding_provider", "huggingface", _emb().model_dump(mode="json")),
        ("cross_encoder_provider", "huggingface-ce", _ce()),
        ("semantic_search_provider", "lance", _ssp_row("lance").model_dump(mode="json")),
    ]


class TestReservedIds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,reserved_id,body", _reserved_cases(), ids=["embedding", "cross-encoder", "semantic-search"])
    async def test_creating_a_reserved_provider_id_is_a_conflict(self, world, kind, reserved_id, body) -> None:
        sp, toolset, _ = world

        is_error, answer = await _call(toolset, f"create_{kind}", entity={**body, "id": reserved_id})

        assert is_error and answer["type"] == "conflict" and "reserved" in answer["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,reserved_id,body", _reserved_cases(), ids=["embedding", "cross-encoder", "semantic-search"])
    async def test_deleting_a_reserved_provider_id_is_forbidden_even_when_no_row_exists(self, world, kind, reserved_id, body) -> None:
        _, toolset, _ = world

        is_error, answer = await _call(toolset, f"delete_{kind}", id=reserved_id)

        assert is_error and answer["type"] == "forbidden", "the delete went on to the row lookup"

    @pytest.mark.asyncio
    async def test_a_reserved_row_that_exists_survives_a_delete(self, world) -> None:
        sp, toolset, _ = world
        row = EmbeddingProvider.model_validate({**_emb().model_dump(mode="json"), "id": "huggingface"})
        await sp.get_storage(EmbeddingProvider).create(row)

        is_error, answer = await _call(toolset, "delete_embedding_provider", id="huggingface")

        assert is_error and answer["type"] == "forbidden"
        assert await sp.get_storage(EmbeddingProvider).get("huggingface") is not None

    @pytest.mark.asyncio
    async def test_updating_a_reserved_row_stays_allowed(self, world) -> None:
        """REST only guards create and delete of a reserved id."""
        sp, toolset, _ = world
        row = EmbeddingProvider.model_validate({**_emb().model_dump(mode="json"), "id": "huggingface"})
        await sp.get_storage(EmbeddingProvider).create(row)
        _, served = await _call(toolset, "get_embedding_provider", id="huggingface")
        served["limits"]["max_concurrency"] = 7

        is_error, answer = await _call(toolset, "update_embedding_provider", id="huggingface", entity=served)

        assert not is_error, answer

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", ["external", "workspace", "workspace_ext"])
    async def test_a_toolset_cannot_take_a_reserved_scope_id(self, world, scope) -> None:
        sp, toolset, _ = world

        is_error, answer = await _call(toolset, "create_toolset", entity={**_toolset_body(), "id": scope})

        assert is_error and answer["type"] == "conflict" and "reserved" in answer["message"]
        assert await sp.get_storage(Toolset).get(scope) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "builtin",
        ["system", "workspaces", "misc", "web", "harness", "trigger", "collections", "crud"],
    )
    async def test_a_toolset_cannot_take_a_built_in_toolset_id(self, world, builtin) -> None:
        """The registry answers these ids from its own providers before it reads storage, so a stored row under one is a ghost
        (ADM-08). The REST route and this tool refuse the same set."""
        sp, toolset, _ = world

        is_error, answer = await _call(toolset, "create_toolset", entity={**_toolset_body(), "id": builtin})

        assert is_error and answer["type"] == "conflict" and "reserved" in answer["message"]
        assert await sp.get_storage(Toolset).get(builtin) is None

    @pytest.mark.asyncio
    async def test_the_default_artifact_provider_cannot_be_deleted_but_other_ids_can(self, world) -> None:
        from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
        from primer.model.providers.artifact import ArtifactStorageProviderType, DbArtifactConfig

        sp, toolset, _ = world
        for provider_id in (DEFAULT_ARTIFACT_PROVIDER_ID, "extra"):
            await sp.get_storage(ArtifactStorageProvider).create(
                ArtifactStorageProvider(id=provider_id, provider=ArtifactStorageProviderType.DB, config=DbArtifactConfig()),
            )

        default_error, default = await _call(toolset, "delete_artifact_storage_provider", id=DEFAULT_ARTIFACT_PROVIDER_ID)
        other_error, _ = await _call(toolset, "delete_artifact_storage_provider", id="extra")

        assert default_error and default["type"] == "forbidden"
        assert await sp.get_storage(ArtifactStorageProvider).get(DEFAULT_ARTIFACT_PROVIDER_ID) is not None
        assert not other_error


class TestReferenceBlocks:
    @pytest.mark.asyncio
    async def test_a_model_profile_an_agent_uses_cannot_be_deleted(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ModelProfile).create(_profile())
        agent = Agent.model_validate({**_agent().model_dump(mode="json"), "model": {"profile_id": "mp-1"}})
        await sp.get_storage(Agent).create(agent)

        is_error, answer = await _call(toolset, "delete_model_profile", id="mp-1")

        assert is_error and answer["type"] == "conflict"
        assert "in_use_by" in answer["message"] and "agt-1" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("mp-1") is not None

    @pytest.mark.asyncio
    async def test_a_model_profile_another_profile_lists_as_a_member_cannot_be_deleted(self, world) -> None:
        sp, toolset, _ = world
        for profile_id in ("mp-1", "mp-2"):
            await sp.get_storage(ModelProfile).create(_profile(profile_id))
        aggregate = ModelProfile(id="agg-1", description="pool", kind="aggregated", members=["mp-1", "mp-2"])
        await sp.get_storage(ModelProfile).create(aggregate)

        is_error, answer = await _call(toolset, "delete_model_profile", id="mp-1")

        assert is_error and answer["type"] == "conflict" and "agg-1" in answer["message"]

    @pytest.mark.asyncio
    async def test_an_unreferenced_model_profile_is_deleted(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ModelProfile).create(_profile())

        is_error, _ = await _call(toolset, "delete_model_profile", id="mp-1")

        assert not is_error
        assert await sp.get_storage(ModelProfile).get("mp-1") is None

    @pytest.mark.asyncio
    async def test_a_channel_provider_a_channel_uses_cannot_be_deleted(self, world) -> None:
        from pydantic import SecretStr

        sp, toolset, _ = world
        provider = ChannelProvider(
            id="cp-1", provider=ChannelProviderType.SLACK,
            config=SlackChannelProviderConfig(app_token=SecretStr("xapp-1-live-0123456789"), bot_token=SecretStr("xoxb-live-9876543210")),
        )
        await sp.get_storage(ChannelProvider).create(provider)
        await sp.get_storage(Channel).create(Channel(id="chan-1", provider_id="cp-1", provider="slack", external_id="C1"))

        is_error, answer = await _call(toolset, "delete_channel_provider", id="cp-1")

        assert is_error and answer["type"] == "conflict" and "chan-1" in answer["message"]
        assert await sp.get_storage(ChannelProvider).get("cp-1") is not None

    @pytest.mark.asyncio
    async def test_a_toolset_a_tool_approval_policy_names_cannot_be_deleted(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(Toolset).create(Toolset.model_validate(_toolset_body()))
        await sp.get_storage(ToolApprovalPolicy).create(_required_policy("ts-1", "some_tool"))

        is_error, answer = await _call(toolset, "delete_toolset", id="ts-1")

        assert is_error and answer["type"] == "conflict" and "tap-1" in answer["message"]
        assert await sp.get_storage(Toolset).get("ts-1") is not None


class TestModelProfileCache:
    @pytest.mark.asyncio
    async def test_updating_a_model_profile_drops_its_cached_aggregated_llm(self, world) -> None:
        sp, toolset, invalidated = world
        await sp.get_storage(ModelProfile).create(_profile())
        _, served = await _call(toolset, "get_model_profile", id="mp-1")
        served["description"] = "edited"

        is_error, _ = await _call(toolset, "update_model_profile", id="mp-1", entity=served)

        assert not is_error
        assert invalidated == ["mp-1"], "the aggregated LLM cached for this profile would be served stale"

    @pytest.mark.asyncio
    async def test_deleting_a_model_profile_drops_it_too(self, world) -> None:
        sp, toolset, invalidated = world
        await sp.get_storage(ModelProfile).create(_profile())

        is_error, _ = await _call(toolset, "delete_model_profile", id="mp-1")

        assert not is_error
        assert invalidated == ["mp-1"]

    @pytest.mark.asyncio
    async def test_creating_one_invalidates_nothing(self, world) -> None:
        _, toolset, invalidated = world

        is_error, _ = await _call(toolset, "create_model_profile", entity=_profile().model_dump(mode="json"))

        assert not is_error
        assert invalidated == []
