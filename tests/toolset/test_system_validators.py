"""The system CRUD tools run the pre-create / pre-update validators the REST routers run (task 01a111d1, D5 phase 2b).

The routers attach per-entity validators to ``make_crud_router`` (``on_pre_create`` / ``on_pre_update``); the generic
``create_`` / ``update_<entity>`` tools stored what those validators refuse (reproduced on a real sqlite store through the real
toolset). Each validator is now ONE shared function over ``(entity, storage_provider)`` that raises a domain error; the REST hook
re-raises exactly what it raised before (the REST tests are unchanged) and the tool maps it to a typed error: ``conflict`` for a
uniqueness clash, ``validation-error`` (naming the field) for a body that is well formed but semantically refused.

One class per entity, in the order the plan ranked them: tool-approval policies first.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.model.channel import Channel, ChannelProvider, ChannelProviderType, SlackChannelProviderConfig
from primer.model.model_profile import ModelProfile
from primer.model.provider import Toolset, ToolsetProviderType
from primer.model.providers.toolset import PythonConfig
from primer.model.tool_approval import (
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from tests.toolset.test_system_crud_guards import _call, _profile, world  # noqa: F401  (world is a fixture; it seeds anthropic-1)

REGO_OK = 'package primer.tool_approval\ndefault required := false\nrequired if input.tool_name == "x"\n'
REGO_BROKEN = "this is not valid rego"


def _policy(policy_id: str = "tap-1", *, toolset_id: str = "ts-1", tool_name: str = "t1", approval=None, enabled: bool = True):
    return ToolApprovalPolicy(
        id=policy_id, toolset_id=toolset_id, tool_name=tool_name, enabled=enabled,
        approval=approval if approval is not None else RequiredApprovalConfig(),
    ).model_dump(mode="json")


class TestToolApprovalPolicy:
    @pytest.mark.asyncio
    async def test_a_second_policy_for_the_same_tool_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-1"))

        is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-2"))

        assert is_error and answer["type"] == "conflict"
        assert "already exists" in answer["message"] and "tap-1" in answer["message"]
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-2") is None, "a duplicate policy was stored"

    @pytest.mark.asyncio
    async def test_an_update_that_collides_with_another_policy_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-1", tool_name="t1"))
        await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-2", tool_name="t2"))

        is_error, answer = await _call(
            toolset, "update_tool_approval_policy", id="tap-2", entity=_policy("tap-2", tool_name="t1"),
        )

        assert is_error and answer["type"] == "conflict" and "tap-1" in answer["message"]
        assert (await sp.get_storage(ToolApprovalPolicy).get("tap-2")).tool_name == "t2"

    @pytest.mark.asyncio
    async def test_a_policy_can_be_updated_in_place(self, world) -> None:
        sp, toolset, _ = world
        await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-1"))

        is_error, _ = await _call(toolset, "update_tool_approval_policy", id="tap-1", entity=_policy("tap-1", enabled=False))

        assert not is_error
        assert (await sp.get_storage(ToolApprovalPolicy).get("tap-1")).enabled is False

    @pytest.mark.asyncio
    async def test_uncompilable_rego_is_refused_on_create(self, world) -> None:
        sp, toolset, _ = world
        body = _policy("tap-rego", approval=PolicyApprovalConfig(policy=REGO_BROKEN))

        is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert is_error and answer["type"] == "validation-error"
        assert "approval.policy" in answer["message"] and "rego compile failed" in answer["message"]
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-rego") is None

    @pytest.mark.asyncio
    async def test_an_update_to_uncompilable_rego_is_refused_and_the_row_is_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-1"))

        is_error, answer = await _call(
            toolset, "update_tool_approval_policy", id="tap-1",
            entity=_policy("tap-1", approval=PolicyApprovalConfig(policy=REGO_BROKEN)),
        )

        assert is_error and answer["type"] == "validation-error" and "approval.policy" in answer["message"]
        assert (await sp.get_storage(ToolApprovalPolicy).get("tap-1")).approval.type == "required"

    @pytest.mark.asyncio
    async def test_valid_required_and_rego_policies_are_created_as_before(self, world) -> None:
        sp, toolset, _ = world

        required_error, _ = await _call(toolset, "create_tool_approval_policy", entity=_policy("tap-1", tool_name="t1"))
        rego_error, _ = await _call(
            toolset, "create_tool_approval_policy",
            entity=_policy("tap-2", tool_name="t2", approval=PolicyApprovalConfig(policy=REGO_OK)),
        )

        assert not (required_error or rego_error)
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-2") is not None

    @pytest.mark.asyncio
    async def test_an_llm_policy_naming_a_missing_provider_is_refused(self, world) -> None:
        sp, toolset, _ = world
        body = _policy("tap-llm", approval=LlmApprovalConfig(provider_id="does-not-exist", model="m", prompt="judge"))

        is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert is_error and answer["type"] == "validation-error"
        assert "approval.provider_id" in answer["message"] and "does-not-exist" in answer["message"]
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-llm") is None

    @pytest.mark.asyncio
    async def test_an_llm_policy_naming_a_model_the_provider_does_not_publish_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ModelProfile).create(_profile("mp-judge", provider_id="anthropic-1", model_name="claude-x"))
        body = _policy("tap-llm", approval=LlmApprovalConfig(provider_id="anthropic-1", model="not-published", prompt="judge"))

        is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert is_error and answer["type"] == "validation-error"
        assert "approval.model" in answer["message"] and "claude-x" in answer["message"], "the message lists what is published"
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-llm") is None

    @pytest.mark.asyncio
    async def test_an_llm_policy_for_a_published_model_is_created_as_before(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ModelProfile).create(_profile("mp-judge", provider_id="anthropic-1", model_name="claude-x"))
        body = _policy("tap-llm", approval=LlmApprovalConfig(provider_id="anthropic-1", model="claude-x", prompt="judge"))

        is_error, _ = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert not is_error
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-llm") is not None


PY_V1 = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return f"hello {name}"
'''
PY_V2 = PY_V1.replace("def greet(", "def salute(")
PY_NO_DOCSTRING = "@primer_tool()\ndef nodoc(x: str) -> str:\n    return x\n"


def _python_toolset(toolset_id: str = "py-1", source: str = PY_V1, version: int = 1) -> dict:
    return Toolset(
        id=toolset_id, provider=ToolsetProviderType.PYTHON,
        config=PythonConfig(source=source, source_version=version, default_timeout_seconds=30),
    ).model_dump(mode="json")


class TestToolset:
    @pytest.mark.asyncio
    async def test_a_python_toolset_whose_source_does_not_register_is_refused_on_create(self, world) -> None:
        sp, toolset, _ = world

        is_error, answer = await _call(toolset, "create_toolset", entity=_python_toolset("py-bad", PY_NO_DOCSTRING))

        assert is_error and answer["type"] == "validation-error" and answer["message"]
        assert await sp.get_storage(Toolset).get("py-bad") is None, "a toolset that cannot register was stored"

    @pytest.mark.asyncio
    async def test_an_update_to_a_source_that_does_not_register_is_refused_and_the_row_is_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(Toolset).create(Toolset.model_validate(_python_toolset()))

        is_error, answer = await _call(
            toolset, "update_toolset", id="py-1", entity=_python_toolset("py-1", PY_NO_DOCSTRING),
        )

        assert is_error and answer["type"] == "validation-error"
        stored = await sp.get_storage(Toolset).get("py-1")
        assert stored.config.source == PY_V1 and stored.config.source_version == 1

    @pytest.mark.asyncio
    async def test_source_version_is_server_owned_when_the_source_changes(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(Toolset).create(Toolset.model_validate(_python_toolset()))

        # The caller claims version 99; the server bumps the stored one, so a session parked in the old code can tell.
        is_error, _ = await _call(toolset, "update_toolset", id="py-1", entity=_python_toolset("py-1", PY_V2, version=99))

        assert not is_error
        stored = await sp.get_storage(Toolset).get("py-1")
        assert stored.config.source == PY_V2 and stored.config.source_version == 2

    @pytest.mark.asyncio
    async def test_source_version_is_kept_when_the_source_is_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(Toolset).create(Toolset.model_validate(_python_toolset()))

        is_error, _ = await _call(toolset, "update_toolset", id="py-1", entity=_python_toolset("py-1", PY_V1, version=99))

        assert not is_error
        assert (await sp.get_storage(Toolset).get("py-1")).config.source_version == 1

    @pytest.mark.asyncio
    async def test_a_valid_python_toolset_is_created_as_before(self, world) -> None:
        sp, toolset, _ = world

        is_error, _ = await _call(toolset, "create_toolset", entity=_python_toolset("py-ok"))

        assert not is_error
        assert await sp.get_storage(Toolset).get("py-ok") is not None

    @pytest.mark.asyncio
    async def test_an_http_mcp_toolset_is_created_without_a_reachability_probe(self, world) -> None:
        # The REST route probes an http/sse MCP endpoint (an 8 s outbound call) and refuses an unreachable one unless
        # ?allow_unreachable. The tool deliberately does NOT (lead's ruling: an outbound call inside an agent turn); pinned so
        # the difference is on the record. Port 9 (discard) refuses the connection, so a probe WOULD have refused this.
        sp, toolset, _ = world
        body = {
            "id": "mcp-dead", "provider": "mcp",
            "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp", "headers": {}}},
        }

        is_error, _ = await _call(toolset, "create_toolset", entity=body)

        assert not is_error
        assert await sp.get_storage(Toolset).get("mcp-dead") is not None


def _aggregate(profile_id: str, members: list[str]) -> ModelProfile:
    return ModelProfile(id=profile_id, description="a pool", kind="aggregated", members=members)


class TestModelProfile:
    """A single profile needs its provider to exist; an aggregate needs two or more distinct, existing, single members and cannot
    name itself; a profile another aggregate lists cannot become an aggregate itself (nested aggregation is not supported)."""

    async def _seed_singles(self, sp, *ids: str) -> None:
        for profile_id in ids:
            await sp.get_storage(ModelProfile).create(_profile(profile_id))

    @pytest.mark.asyncio
    async def test_a_single_profile_naming_a_missing_provider_is_refused(self, world) -> None:
        sp, toolset, _ = world

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_profile("mp-dangling", provider_id="no-such-provider").model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error"
        assert "provider_id" in answer["message"] and "no-such-provider" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("mp-dangling") is None

    @pytest.mark.asyncio
    async def test_an_update_to_a_missing_provider_is_refused_and_the_row_is_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1")

        is_error, answer = await _call(
            toolset, "update_model_profile", id="mp-1", entity=_profile("mp-1", provider_id="no-such-provider").model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "provider_id" in answer["message"]
        assert (await sp.get_storage(ModelProfile).get("mp-1")).provider_id == "anthropic-1"

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_fewer_than_two_members_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1")

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-one", ["mp-1"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "members" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("agg-one") is None

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_itself_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1")

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-self", ["agg-self", "mp-1"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "cannot name itself" in answer["message"]

    @pytest.mark.asyncio
    async def test_an_aggregate_with_a_duplicate_member_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1")

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-dup", ["mp-1", "mp-1"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "duplicates" in answer["message"]

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_a_missing_member_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1")

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-missing", ["mp-1", "mp-ghost"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "mp-ghost" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("agg-missing") is None

    @pytest.mark.asyncio
    async def test_an_aggregate_naming_an_aggregate_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1", "mp-2")
        await sp.get_storage(ModelProfile).create(_aggregate("agg-inner", ["mp-1", "mp-2"]))

        is_error, answer = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-outer", ["agg-inner", "mp-1"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "nested aggregation" in answer["message"]
        assert await sp.get_storage(ModelProfile).get("agg-outer") is None

    @pytest.mark.asyncio
    async def test_a_member_cannot_become_an_aggregate_while_another_aggregate_lists_it(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1", "mp-2", "mp-3")
        await sp.get_storage(ModelProfile).create(_aggregate("agg-1", ["mp-1", "mp-2"]))

        is_error, answer = await _call(
            toolset, "update_model_profile", id="mp-1", entity=_aggregate("mp-1", ["mp-2", "mp-3"]).model_dump(mode="json"),
        )

        assert is_error and answer["type"] == "validation-error" and "agg-1" in answer["message"]
        assert (await sp.get_storage(ModelProfile).get("mp-1")).kind == "single"

    @pytest.mark.asyncio
    async def test_valid_single_and_aggregated_profiles_are_created_and_updated_as_before(self, world) -> None:
        sp, toolset, _ = world
        await self._seed_singles(sp, "mp-1", "mp-2")

        single_error, _ = await _call(toolset, "create_model_profile", entity=_profile("mp-3").model_dump(mode="json"))
        aggregate_error, _ = await _call(
            toolset, "create_model_profile", entity=_aggregate("agg-ok", ["mp-1", "mp-2"]).model_dump(mode="json"),
        )
        _, served = await _call(toolset, "get_model_profile", id="mp-1")
        served["description"] = "edited"
        update_error, _ = await _call(toolset, "update_model_profile", id="mp-1", entity=served)

        assert not (single_error or aggregate_error or update_error)
        assert (await sp.get_storage(ModelProfile).get("mp-1")).description == "edited"


def _slack_provider(provider_id: str) -> ChannelProvider:
    return ChannelProvider(
        id=provider_id, provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(app_token=SecretStr("xapp-1-live-0123456789"), bot_token=SecretStr("xoxb-live-9876543210")),
    )


def _channel(channel_id: str, provider_id: str = "cp-1", external_id: str = "C1", platform: str = "slack") -> dict:
    return Channel(id=channel_id, provider_id=provider_id, provider=platform, external_id=external_id).model_dump(mode="json")


class TestChannel:
    @pytest.mark.asyncio
    async def test_a_channel_naming_a_missing_provider_is_refused(self, world) -> None:
        sp, toolset, _ = world

        is_error, answer = await _call(toolset, "create_channel", entity=_channel("chan-x", provider_id="cp-ghost"))

        assert is_error and answer["type"] == "validation-error"
        assert "ChannelProvider" in answer["message"] and "cp-ghost" in answer["message"]
        assert await sp.get_storage(Channel).get("chan-x") is None, "a channel naming a missing provider was stored"

    @pytest.mark.asyncio
    async def test_a_second_channel_for_the_same_provider_and_external_id_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1")))

        is_error, answer = await _call(toolset, "create_channel", entity=_channel("chan-2"))

        assert is_error and answer["type"] == "conflict"
        assert "already exists" in answer["message"] and "chan-1" in answer["message"]
        assert await sp.get_storage(Channel).get("chan-2") is None

    @pytest.mark.asyncio
    async def test_a_valid_channel_is_created_as_before(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))

        is_error, _ = await _call(toolset, "create_channel", entity=_channel("chan-1"))

        assert not is_error
        assert await sp.get_storage(Channel).get("chan-1") is not None

    @pytest.mark.asyncio
    async def test_the_same_external_id_under_another_provider_is_allowed(self, world) -> None:
        # Uniqueness is per (provider_id, external_id), not per external_id.
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-2"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1", provider_id="cp-1")))

        is_error, _ = await _call(toolset, "create_channel", entity=_channel("chan-2", provider_id="cp-2"))

        assert not is_error
        assert await sp.get_storage(Channel).get("chan-2") is not None

    @pytest.mark.asyncio
    async def test_a_channel_can_be_updated_as_before(self, world) -> None:
        # An update that keeps the provider and its platform is accepted (the served row round-trips).
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1")))
        _, served = await _call(toolset, "get_channel", id="chan-1")

        is_error, _ = await _call(toolset, "update_channel", id="chan-1", entity=served)

        assert not is_error

    @pytest.mark.asyncio
    async def test_a_channel_naming_another_platform_than_its_provider_is_refused(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))

        is_error, answer = await _call(toolset, "create_channel", entity=_channel("chan-x", platform="telegram"))

        assert is_error and answer["type"] == "validation-error"
        assert all(part in answer["message"] for part in ("telegram", "slack", "cp-1")), "the message names both platforms"
        assert await sp.get_storage(Channel).get("chan-x") is None, "a mismatched channel was stored"

    @pytest.mark.asyncio
    async def test_an_update_to_another_platform_is_refused_and_the_row_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1")))

        is_error, answer = await _call(
            toolset, "update_channel", id="chan-1", entity=_channel("chan-1", platform="discord"),
        )

        assert is_error and answer["type"] == "validation-error"
        assert "discord" in answer["message"] and "slack" in answer["message"]
        assert (await sp.get_storage(Channel).get("chan-1")).provider == ChannelProviderType.SLACK

    @pytest.mark.asyncio
    async def test_an_update_naming_a_missing_provider_is_refused_and_the_row_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1")))

        is_error, answer = await _call(
            toolset, "update_channel", id="chan-1", entity=_channel("chan-1", provider_id="cp-ghost"),
        )

        assert is_error and answer["type"] == "validation-error" and "cp-ghost" in answer["message"]
        assert (await sp.get_storage(Channel).get("chan-1")).provider_id == "cp-1"

    @pytest.mark.asyncio
    async def test_an_update_onto_another_channels_pair_is_refused_and_the_row_unchanged(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1", external_id="C1")))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-2", external_id="C2")))

        is_error, answer = await _call(
            toolset, "update_channel", id="chan-2", entity=_channel("chan-2", external_id="C1"),
        )

        assert is_error and answer["type"] == "conflict"
        assert "already exists" in answer["message"] and "chan-1" in answer["message"]
        assert (await sp.get_storage(Channel).get("chan-2")).external_id == "C2", "the refused update changed the row"

    @pytest.mark.asyncio
    async def test_an_update_that_keeps_its_own_pair_does_not_conflict_with_itself(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1", external_id="C1")))
        body = _channel("chan-1", external_id="C1")
        body["label"] = "renamed"

        is_error, _ = await _call(toolset, "update_channel", id="chan-1", entity=body)

        assert not is_error
        assert (await sp.get_storage(Channel).get("chan-1")).label == "renamed"

    @pytest.mark.asyncio
    async def test_an_update_to_the_same_external_id_under_another_provider_is_allowed(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-1"))
        await sp.get_storage(ChannelProvider).create(_slack_provider("cp-2"))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-1", provider_id="cp-1", external_id="C1")))
        await sp.get_storage(Channel).create(Channel.model_validate(_channel("chan-2", provider_id="cp-1", external_id="C2")))

        is_error, _ = await _call(
            toolset, "update_channel", id="chan-2", entity=_channel("chan-2", provider_id="cp-2", external_id="C1"),
        )

        assert not is_error
        assert (await sp.get_storage(Channel).get("chan-2")).provider_id == "cp-2"
