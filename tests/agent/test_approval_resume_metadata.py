"""``approval_resume_metadata`` is the one builder of an approval park's resume_metadata (ticket 01a11b64, follow-up of #536).

It carries the keys other code trusts: ``approvers`` (who may decide), ``policy_id``, ``approval_type``, ``gate_reason`` and ``original_call``.
A site's own keys ride in ``extra``; an ``extra`` key that collides with one of those must not be able to overwrite it (a typo or a future
site could otherwise replace the stamped approvers), so a collision is refused loudly.
"""

from __future__ import annotations

import pytest

from primer.agent.approval import ApprovalVerdict, approval_resume_metadata
from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy

CALL = {"id": "c1", "name": "echo", "arguments": {"x": 1}}


def _policy(**fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id="p-1", toolset_id="_test", tool_name="echo", approval=RequiredApprovalConfig(), **fields)


def test_the_metadata_carries_the_gate_the_call_and_who_may_decide():
    alice = ApproverSpec(kind="users", users=["alice"])

    meta = approval_resume_metadata(
        policy=_policy(approvers=alice), verdict=ApprovalVerdict(required=True, reason="because"), original_call=CALL,
        via_call_tool={"toolset_id": "system"},
    )

    assert meta == {
        "policy_id": "p-1", "approval_type": "required", "gate_reason": "because", "approvers": alice.model_dump(),
        "via_call_tool": {"toolset_id": "system"}, "original_call": CALL,
    }


def test_the_verdicts_per_call_routing_wins_over_the_policys_and_none_is_stamped_explicitly():
    carol = ApproverSpec(kind="users", users=["carol"])

    routed = approval_resume_metadata(
        policy=_policy(approvers=ApproverSpec(kind="users", users=["alice"])), verdict=ApprovalVerdict(required=True, approvers=carol),
        original_call=CALL,
    )
    open_ = approval_resume_metadata(policy=_policy(), verdict=ApprovalVerdict(required=True), original_call=CALL)

    assert routed["approvers"] == carol.model_dump()
    assert "approvers" in open_ and open_["approvers"] is None


# `original_call`, `policy` and `verdict` are named parameters, so Python itself refuses a second value for them; the stamped keys that
# `extra` could reach are these.
@pytest.mark.parametrize("key", ["approvers", "policy_id", "approval_type", "gate_reason"])
def test_an_extra_key_cannot_overwrite_a_stamped_one(key):
    with pytest.raises(ValueError, match=key):
        approval_resume_metadata(
            policy=_policy(approvers=ApproverSpec(kind="users", users=["alice"])), verdict=ApprovalVerdict(required=True),
            original_call=CALL, **{key: "forged"},
        )


def test_a_policy_type_does_not_change_the_shape():
    meta = approval_resume_metadata(
        policy=ToolApprovalPolicy(id="p-2", toolset_id="_test", tool_name="echo", approval=PolicyApprovalConfig(policy="package x")),
        verdict=ApprovalVerdict(required=True), original_call=CALL,
    )

    assert meta["approval_type"] == "policy" and meta["policy_id"] == "p-2"
