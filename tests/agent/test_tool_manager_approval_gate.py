"""Verify ToolExecutionManager parks via YieldToWorker when a policy gates a call."""

from __future__ import annotations

import pytest

from primer.agent.approval import ApprovalResolver
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.yield_ import YieldToWorker


class _PoliciesOnlyResolver(ApprovalResolver):
    """Test resolver bypassing storage."""

    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):
        for p in self._policies:
            if p.toolset_id == toolset_id and p.tool_name == tool_name:
                return p
        return None


# The catalog scopes tool names as ``toolset_id__bare_name``, so the LLM
# (and these tests) use ``_test__echo`` as the call name.
_SCOPED_NAME = "_test__echo"


@pytest.mark.asyncio
async def test_execute_raises_yield_to_worker_on_required_policy(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )
    from primer.model.chat import ToolCallPart

    with pytest.raises(YieldToWorker) as ei:
        await tm.execute(ToolCallPart(id="c1", name=_SCOPED_NAME, arguments={"x": 1}))
    yielded = ei.value.yielded
    assert yielded.tool_name == "_approval"
    assert "tool_approval:" in yielded.event_key
    md = yielded.resume_metadata
    assert md["original_call"]["name"] == _SCOPED_NAME
    assert md["original_call"]["arguments"] == {"x": 1}


@pytest.mark.asyncio
async def test_execute_bypass_approval_skips_gate(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )
    from primer.model.chat import ToolCallPart

    result = await tm.execute(
        ToolCallPart(id="c2", name=_SCOPED_NAME, arguments={"x": 2}),
        bypass_approval=True,
    )
    assert result.id == "c2"


@pytest.mark.asyncio
async def test_execute_no_policy_dispatches_normally(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver([])
    from primer.model.chat import ToolCallPart

    result = await tm.execute(
        ToolCallPart(id="c3", name=_SCOPED_NAME, arguments={"x": 3}),
    )
    assert result.id == "c3"
