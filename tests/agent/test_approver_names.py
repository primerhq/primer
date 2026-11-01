"""GI-21: an approver named as typed ("Alice") matches the stored username, and a policy naming an unknown role is refused at save."""

from __future__ import annotations

import pytest

from primer.agent.approval_checks import check_approval_config
from primer.api.routers.admin_users import _normalise_username
from primer.common.entity_checks import EntityCheckError
from primer.model.tool_approval import ApproverSpec, ToolApprovalPolicy
from primer.session.approvers import may_decide


def _policy(approvers: dict) -> ToolApprovalPolicy:
    return ToolApprovalPolicy.model_validate(
        {"id": "p1", "toolset_id": "system", "tool_name": "delete_agent", "approval": {"type": "required"}, "approvers": approvers}
    )


def test_a_capitalised_approver_matches_the_stored_username() -> None:
    policy = _policy({"kind": "users", "users": ["Alice"]})
    stored = _normalise_username("Alice")
    assert stored == "alice" and policy.approvers.users == ["alice"]
    assert may_decide({"approvers": policy.approvers.model_dump()}, username=stored, role="user") is True


def test_a_spec_stored_before_the_fix_still_matches() -> None:
    # `model_construct` skips validation: the shape an old row has when it is read back as stored.
    spec = ApproverSpec.model_construct(kind="users", roles=[], users=["Alice"])
    assert spec.allows(username="alice", role="user") is True
    assert spec.allows(username=" ALICE ", role="user") is True


def test_other_users_and_roles_are_still_refused() -> None:
    spec = ApproverSpec(kind="users", users=["alice"])
    assert spec.allows(username="bob", role="user") is False
    assert spec.allows(username="root", role="admin") is True
    assert ApproverSpec(kind="roles", roles=["user"]).allows(username="bob", role="restricted") is False


@pytest.mark.asyncio
async def test_a_misspelled_role_is_refused_at_save() -> None:
    with pytest.raises(EntityCheckError) as caught:
        await check_approval_config(_policy({"kind": "roles", "roles": ["users"]}), storage_provider=None)
    assert caught.value.kind == "validation" and caught.value.field == "approvers.roles"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "user", "restricted"])
async def test_real_roles_pass_the_save_check(role) -> None:
    await check_approval_config(_policy({"kind": "roles", "roles": [role]}), storage_provider=None)
