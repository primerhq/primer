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

from primer.model.model_profile import ModelProfile
from primer.model.provider import LLMProvider
from primer.model.tool_approval import (
    LlmApprovalConfig,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from tests.toolset.test_system import _llm
from tests.toolset.test_system_crud_guards import _call, _profile, world  # noqa: F401  (world is a fixture)

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
        await sp.get_storage(LLMProvider).create(_llm())
        await sp.get_storage(ModelProfile).create(_profile("mp-judge", provider_id="anthropic-1", model_name="claude-x"))
        body = _policy("tap-llm", approval=LlmApprovalConfig(provider_id="anthropic-1", model="not-published", prompt="judge"))

        is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert is_error and answer["type"] == "validation-error"
        assert "approval.model" in answer["message"] and "claude-x" in answer["message"], "the message lists what is published"
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-llm") is None

    @pytest.mark.asyncio
    async def test_an_llm_policy_for_a_published_model_is_created_as_before(self, world) -> None:
        sp, toolset, _ = world
        await sp.get_storage(LLMProvider).create(_llm())
        await sp.get_storage(ModelProfile).create(_profile("mp-judge", provider_id="anthropic-1", model_name="claude-x"))
        body = _policy("tap-llm", approval=LlmApprovalConfig(provider_id="anthropic-1", model="claude-x", prompt="judge"))

        is_error, _ = await _call(toolset, "create_tool_approval_policy", entity=body)

        assert not is_error
        assert await sp.get_storage(ToolApprovalPolicy).get("tap-llm") is not None
