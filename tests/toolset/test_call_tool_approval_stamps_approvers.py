"""The ``call_tool`` approval park stamps the effective approvers (ticket 01a11b64).

``call_tool`` parks for approval exactly as the agent loop does, but its ``resume_metadata`` omitted ``approvers``, so the respond route's
check (which reads the stamp) found none and let ANY user decide a gated tool, whatever the policy says, including the admin-only fallback
for duplicate policies. Every approval park now builds its metadata through ``approval_resume_metadata``, so the stamp cannot be forgotten
again by a third park site.
"""

from __future__ import annotations

import pytest

from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker
from primer.toolset.system import SYSTEM_TOOLSET_ID

# Re-export so pytest can resolve the real-sqlite-free system toolset fixtures and the helpers of the neighbouring call_tool tests.
from tests.toolset.test_system import _ctx, _llm, pr, sp, system_toolset  # noqa: F401

ALICE = ApproverSpec(kind="users", users=["alice"])
ROUTES_TO_CAROL = (
    "package primer.tool_approval\n"
    "default required := true\n"
    "approvers := {\"kind\": \"users\", \"users\": [\"carol\"]}\n"
)


async def _park(system_toolset, sp, policy: ToolApprovalPolicy) -> dict:
    """Store ``policy`` for the inner tool, call it through ``call_tool`` and return the park's resume_metadata."""
    await sp.get_storage(ToolApprovalPolicy).create(policy)
    await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": _llm().model_dump(mode="json")})
    with pytest.raises(YieldToWorker) as parked:
        await system_toolset.call(
            tool_name="call_tool",
            arguments={"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}},
            ctx=_ctx(),
        )
    return parked.value.yielded.resume_metadata


def _policy(**fields) -> ToolApprovalPolicy:
    base = dict(id="tap-1", toolset_id=SYSTEM_TOOLSET_ID, tool_name="get_llm_provider", enabled=True, approval=RequiredApprovalConfig())
    base.update(fields)
    return ToolApprovalPolicy(**base)


@pytest.mark.asyncio
async def test_the_park_stamps_the_policys_approvers(system_toolset, sp) -> None:
    meta = await _park(system_toolset, sp, _policy(approvers=ALICE))

    assert meta["approvers"] == ALICE.model_dump(), "the park did not stamp who may decide it, so the respond route cannot enforce it"
    assert meta["policy_id"] == "tap-1" and meta["via_call_tool"]["toolset_id"] == SYSTEM_TOOLSET_ID


@pytest.mark.asyncio
async def test_the_per_call_routing_of_a_conditional_verdict_wins_over_the_policys_own(system_toolset, sp) -> None:
    meta = await _park(system_toolset, sp, _policy(approval=PolicyApprovalConfig(policy=ROUTES_TO_CAROL), approvers=ALICE))

    assert meta["approvers"] == ApproverSpec(kind="users", users=["carol"]).model_dump()


@pytest.mark.asyncio
async def test_a_policy_without_approvers_stamps_none_explicitly(system_toolset, sp) -> None:
    """None means anyone; the key is still present, as the agent loop's park writes it."""
    meta = await _park(system_toolset, sp, _policy())

    assert "approvers" in meta and meta["approvers"] is None
