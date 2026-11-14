"""S5 P1: every crud tool, and every other door to the same writes (audit G6-1), is approval-gated by default, and stays operator-editable."""
from __future__ import annotations

from primer.agent.approval import ApprovalResolver
from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.bootstrap.seed import crud_policy_id, default_policy_id, ensure_crud_approval_policies
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.toolset.admin_gates import ADMIN_GATES, HOST_CODE_TOOLSET_POLICY
from primer.toolset.crud import CRUD_GATED_TOOL_NAMES, CRUD_TWINS, DEFAULT_GATED_TOOLS


async def test_seeds_one_policy_per_default_gated_tool(fake_storage_provider):
    """Pin the complete production gate universe, including generated REST mutators."""
    app = create_app(AppConfig())
    generated_mutators = {
        (operation.tool.toolset_id, operation.tool.id)
        for operation in app.state.generated_route_toolsets.operations
        if not operation.tool.read_only
    }
    curated_gates = {
        *(("crud", name) for name in CRUD_GATED_TOOL_NAMES),
        *CRUD_TWINS,
        *ADMIN_GATES,
    }

    created = await ensure_crud_approval_policies(fake_storage_provider)
    assert set(created) == {default_policy_id(t, n) for t, n in DEFAULT_GATED_TOOLS}
    assert len(generated_mutators) == 128
    assert {("auth", "acknowledge_welcome"), ("auth", "reopen_welcome")} <= generated_mutators
    assert len(curated_gates) == 28
    assert set(DEFAULT_GATED_TOOLS) == generated_mutators | curated_gates
    assert {crud_policy_id(n) for n in CRUD_GATED_TOOL_NAMES} <= set(created), "the crud rows keep their ids"
    store = fake_storage_provider.get_storage(ToolApprovalPolicy)
    for name in ("acknowledge_welcome", "reopen_welcome"):
        welcome_gate = await store.get(default_policy_id("auth", name))
        assert welcome_gate.enabled is True
        assert welcome_gate.approval.type.value == "required"
        assert welcome_gate.approvers is None, "a user's own welcome uses the ordinary user-tier gate"
    twin = await store.get(default_policy_id("system", "update_agent"))
    assert (twin.toolset_id, twin.tool_name, twin.enabled, twin.approval.type.value) == ("system", "update_agent", True, "required")
    assert len(CRUD_GATED_TOOL_NAMES) == 8
    assert len(CRUD_TWINS) == 9
    assert len(ADMIN_GATES) == 11
    assert len(DEFAULT_GATED_TOOLS) == len(set(created)) == 153
    for (toolset_id, tool_name), shipped in ADMIN_GATES.items():
        row = await store.get(default_policy_id(toolset_id, tool_name))
        assert (row.toolset_id, row.tool_name, row.enabled, row.approval) == (toolset_id, tool_name, True, shipped)
        assert row.approvers is not None and row.approvers.model_dump() == ADMIN_ONLY_SPEC, "an admin-tier gate is decided by an admin only"
    for toolset_id, tool_name in (*(("crud", n) for n in CRUD_GATED_TOOL_NAMES), *CRUD_TWINS):
        assert (await store.get(default_policy_id(toolset_id, tool_name))).approvers is None, "the construction gates keep anyone"
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


async def test_a_fresh_install_does_not_gate_the_read_only_list_python_tools(fake_storage_provider):
    """Ticket 01a16f1d ruling 4: ``crud__list_python_tools`` only reads (it derives a Python toolset's tools from the stored source and writes
    nothing), so no shipped gate parks it. The seed gated it ``required`` only because it gated every tool of the crud toolset, whose gates are for
    the builder's platform writes; the two Python toolset writes beside it stay gated."""
    from primer.bootstrap.seed import policy_rows_for
    from primer.toolset.crud import shipped_gate
    from primer.toolset.tool_catalog import default_gate

    created = await ensure_crud_approval_policies(fake_storage_provider)

    assert ("crud", "list_python_tools") not in DEFAULT_GATED_TOOLS
    assert shipped_gate("crud", "list_python_tools") is None and default_gate("crud", "list_python_tools") is None
    assert await policy_rows_for(fake_storage_provider.get_storage(ToolApprovalPolicy), "crud", "list_python_tools") == []
    assert crud_policy_id("list_python_tools") not in created
    assert {crud_policy_id("create_python_toolset"), crud_policy_id("update_python_toolset_source")} <= set(created)
