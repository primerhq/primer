"""S5 P1: every crud tool, and every other door to the same writes (audit G6-1), is approval-gated by default, and stays operator-editable."""
from __future__ import annotations

from primer.agent.approval import ApprovalResolver
from primer.bootstrap.seed import crud_policy_id, default_policy_id, ensure_crud_approval_policies
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.toolset.admin_gates import ADMIN_GATES, HOST_CODE_TOOLSET_POLICY
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS, DEFAULT_GATED_TOOLS


async def test_seeds_one_policy_per_default_gated_tool(fake_storage_provider):
    """The crud tools and their twins (the system and trigger tools that make the same writes, and the deletes), then the admin-tier writes (the
    policy writes, the toolset writes that run code on the host, the harness lifecycle; ticket 01a16a29): this pinned the crud nine only, then 18."""
    created = await ensure_crud_approval_policies(fake_storage_provider)
    assert set(created) == {default_policy_id(t, n) for t, n in DEFAULT_GATED_TOOLS}
    assert {crud_policy_id(n) for n in CRUD_TOOL_NAMES} <= set(created), "the crud rows keep their ids"
    store = fake_storage_provider.get_storage(ToolApprovalPolicy)
    twin = await store.get(default_policy_id("system", "update_agent"))
    assert (twin.toolset_id, twin.tool_name, twin.enabled, twin.approval.type.value) == ("system", "update_agent", True, "required")
    assert len(CRUD_TWINS) == 9 and len(ADMIN_GATES) == 8 and len(set(created)) == 26
    for (toolset_id, tool_name), shipped in ADMIN_GATES.items():
        row = await store.get(default_policy_id(toolset_id, tool_name))
        assert (row.toolset_id, row.tool_name, row.enabled, row.approval) == (toolset_id, tool_name, True, shipped)
    toolset_gate = await store.get(default_policy_id("system", "create_toolset"))
    assert toolset_gate.approval == PolicyApprovalConfig(policy=HOST_CODE_TOOLSET_POLICY)
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


async def test_a_tool_that_already_has_a_policy_under_another_id_gets_no_second_one(fake_storage_provider):
    """An operator's own policy for a default-gated tool, under any id and either spelling of the toolset, is that tool's gate: a seeded row beside it
    would be a duplicate, which the gate settles as unconditional and decided by an admin only."""
    store = fake_storage_provider.get_storage(ToolApprovalPolicy)
    rego = PolicyApprovalConfig(policy="package primer.tool_approval\ndefault required := false\n")
    await store.create(ToolApprovalPolicy(id="mine-1", toolset_id="_system", tool_name="update_agent", approval=rego))
    await store.create(ToolApprovalPolicy(id="mine-2", toolset_id="crud", tool_name="create_agent", enabled=False, approval=RequiredApprovalConfig()))

    created = await ensure_crud_approval_policies(fake_storage_provider)

    assert default_policy_id("system", "update_agent") not in created and crud_policy_id("create_agent") not in created
    assert await store.get(default_policy_id("system", "update_agent")) is None
    assert await store.get(crud_policy_id("create_agent")) is None
    assert default_policy_id("system", "create_agent") in created, "other tools are still seeded"
