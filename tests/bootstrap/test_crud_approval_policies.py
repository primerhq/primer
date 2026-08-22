"""S5 P1: every crud tool is approval-gated by default, and stays operator-editable."""
from __future__ import annotations

from primer.agent.approval import ApprovalResolver
from primer.bootstrap.seed import crud_policy_id, ensure_crud_approval_policies
from primer.model.tool_approval import ToolApprovalPolicy
from primer.toolset.crud import CRUD_TOOL_NAMES


async def test_seeds_one_required_policy_per_crud_tool(fake_storage_provider):
    created = await ensure_crud_approval_policies(fake_storage_provider)
    assert set(created) == {crud_policy_id(n) for n in CRUD_TOOL_NAMES}
    store = fake_storage_provider.get_storage(ToolApprovalPolicy)
    row = await store.get(crud_policy_id("create_agent"))
    assert row.toolset_id == "crud"
    assert row.tool_name == "create_agent"
    assert row.enabled is True
    assert row.approval.type.value == "required"


async def test_second_pass_creates_nothing(fake_storage_provider):
    await ensure_crud_approval_policies(fake_storage_provider)
    assert await ensure_crud_approval_policies(fake_storage_provider) == []


async def test_an_operator_relaxation_survives_the_next_pass(fake_storage_provider):
    await ensure_crud_approval_policies(fake_storage_provider)
    store = fake_storage_provider.get_storage(ToolApprovalPolicy)
    row = await store.get(crud_policy_id("create_agent"))
    row.enabled = False
    await store.update(row)
    await ensure_crud_approval_policies(fake_storage_provider)
    assert (await store.get(crud_policy_id("create_agent"))).enabled is False


async def test_the_resolver_finds_the_seeded_gate(fake_storage_provider):
    await ensure_crud_approval_policies(fake_storage_provider)
    resolver = ApprovalResolver(
        storage=fake_storage_provider.get_storage(ToolApprovalPolicy),
    )
    found = await resolver.find(toolset_id="crud", tool_name="create_agent")
    assert found is not None and found.approval.type.value == "required"
