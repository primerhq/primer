"""The system ``create_`` / ``update_tool_approval_policy`` tools run the same ``preview_args`` check as the REST route (design 01a11cd3-66b0, condition (c)).

The tool answers a typed ``validation-error`` whose message leads with the field (``EntityCheckError.tool_message``), the same sentence the REST body carries
(``tests/api/test_policy_preview_args_check.py``). The write does not happen.
"""

from __future__ import annotations

import pytest

from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)


def _policy(policy_id: str = "tap-pv", *, tool_name: str = "get_llm_provider", toolset_id: str = "system", **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=policy_id, toolset_id=toolset_id, tool_name=tool_name, approval=RequiredApprovalConfig(), **fields)


@pytest.mark.asyncio
async def test_a_path_the_tool_has_is_accepted_by_the_tool(world) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=_policy(preview_args=["id"]))

    assert not is_error, answer
    assert (await sp.get_storage(ToolApprovalPolicy).get("tap-pv")).preview_args == ["id"]


@pytest.mark.asyncio
async def test_a_path_the_tool_lacks_is_a_validation_error_leading_with_the_field(world) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=_policy(preview_args=["nope"]))

    assert is_error and answer["type"] == "validation-error"
    assert answer["message"] == (
        "preview_args: preview_args ['nope'] name no argument of the tool 'get_llm_provider' of toolset 'system' (top-level arguments: ['id'])"
    )
    assert await sp.get_storage(ToolApprovalPolicy).get("tap-pv") is None


@pytest.mark.asyncio
async def test_a_tool_that_is_not_in_the_catalogue_is_a_validation_error_when_paths_are_named(world) -> None:
    _, toolset, _ = world

    is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=_policy(tool_name="no_such_tool", preview_args=["id"]))

    assert is_error and answer["type"] == "validation-error"
    assert answer["message"] == (
        "preview_args: tool 'no_such_tool' of toolset 'system' is not in the catalogue right now, so preview_args cannot be checked against its arguments; "
        "set preview_args once the tool is reachable"
    )


@pytest.mark.asyncio
async def test_a_policy_with_no_paths_on_an_unknown_tool_is_still_created(world) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call(toolset, "create_tool_approval_policy", entity=_policy(tool_name="no_such_tool"))

    assert not is_error, answer
    assert (await sp.get_storage(ToolApprovalPolicy).get("tap-pv")).preview_args is None


@pytest.mark.asyncio
async def test_an_update_to_a_path_the_tool_lacks_is_refused_and_the_row_is_unchanged(world) -> None:
    sp, toolset, _ = world
    await _call(toolset, "create_tool_approval_policy", entity=_policy(preview_args=["id"]))

    is_error, answer = await _call(toolset, "update_tool_approval_policy", id="tap-pv", entity=_policy(preview_args=["nope"]))

    assert is_error and answer["type"] == "validation-error" and "name no argument of the tool" in answer["message"]
    assert (await sp.get_storage(ToolApprovalPolicy).get("tap-pv")).preview_args == ["id"]
