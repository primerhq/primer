"""The ``call_tool`` approval park stamps the effective approvers (ticket 01a11b64).

``call_tool`` parks for approval exactly as the agent loop does, but its ``resume_metadata`` omitted ``approvers``, so the respond route's
check (which reads the stamp) found none and let ANY user decide a gated tool, whatever the policy says, including the admin-only fallback
for duplicate policies. Every approval park now builds its metadata through ``approval_resume_metadata``, so the stamp cannot be forgotten
again by a third park site.
"""

from __future__ import annotations

import pytest

from primer.model.common import dump_for_storage
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
    await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": dump_for_storage(_llm())})
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
async def test_the_per_call_routing_of_a_conditional_verdict_routes_a_policy_with_no_approvers(system_toolset, sp) -> None:
    meta = await _park(system_toolset, sp, _policy(approval=PolicyApprovalConfig(policy=ROUTES_TO_CAROL)))

    assert meta["approvers"] == ApproverSpec(kind="users", users=["carol"]).model_dump()


@pytest.mark.asyncio
async def test_a_verdict_narrows_the_policys_approvers_and_never_replaces_them(system_toolset, sp) -> None:
    """The effective spec is the STRICTER of the row's and the verdict's (lead ruling R2-4 on #860): the decider must satisfy both.

    A verdict naming someone the row does not admit cannot hand the gate to them; with nobody left, only an admin decides."""
    meta = await _park(system_toolset, sp, _policy(approval=PolicyApprovalConfig(policy=ROUTES_TO_CAROL), approvers=ALICE))

    assert meta["approvers"] == ApproverSpec(kind="users", users=[]).model_dump()


@pytest.mark.asyncio
async def test_a_verdict_picks_the_approvers_the_policy_also_admits(system_toolset, sp) -> None:
    both = ApproverSpec(kind="users", users=["alice", "carol"])
    meta = await _park(system_toolset, sp, _policy(approval=PolicyApprovalConfig(policy=ROUTES_TO_CAROL), approvers=both))

    assert meta["approvers"] == ApproverSpec(kind="users", users=["carol"]).model_dump()


@pytest.mark.asyncio
async def test_a_policy_without_approvers_stamps_none_explicitly(system_toolset, sp) -> None:
    """None means anyone; the key is still present, as the agent loop's park writes it."""
    meta = await _park(system_toolset, sp, _policy())

    assert "approvers" in meta and meta["approvers"] is None
