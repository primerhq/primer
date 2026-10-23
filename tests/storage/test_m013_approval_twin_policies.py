"""Migration 13: an upgraded install gates every twin of a crud tool as its crud sibling is gated NOW (audit G6-1).

Run on SQLite through the live policy model, so a row the migration writes is read back the way the gate reads it. These tests pin WHAT the
migration writes and leaves; the runner is pinned by ``tests/storage/test_migrations.py`` and the gates themselves, through every door, by
``tests/toolset/test_approval_twins.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.bootstrap.seed import crud_policy_id, default_policy_id
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m013_approval_twin_policies import M013ApprovalTwinPolicies
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS

_REGO = "package primer.tool_approval\ndefault required := false\nrequired if input.arguments.id == \"operator\"\n"


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m013.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _old_seed(sp) -> None:
    """What the seed wrote before the twins were gated: one required policy per crud tool."""
    store = sp.get_storage(ToolApprovalPolicy)
    for name in CRUD_TOOL_NAMES:
        await store.create(ToolApprovalPolicy(id=crud_policy_id(name), toolset_id="crud", tool_name=name, approval=RequiredApprovalConfig()))


async def _rows(sp) -> dict[str, ToolApprovalPolicy]:
    page = await sp.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=100))
    return {row.id: row for row in page.items}


def test_it_is_registered_as_version_13():
    assert any(isinstance(m, M013ApprovalTwinPolicies) and m.version == 13 for m in MIGRATIONS)


async def test_an_untouched_install_gets_the_shipped_gate_on_every_twin(sp):
    await _old_seed(sp)

    await M013ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    for toolset_id, tool_name in CRUD_TWINS:
        row = rows[default_policy_id(toolset_id, tool_name)]
        assert (row.toolset_id, row.tool_name, row.enabled, row.approval.type.value) == (toolset_id, tool_name, True, "required")
        assert row.approvers is None and row.timeout_seconds is None and row.preview_args is None
    assert len(rows) == len(CRUD_TOOL_NAMES) + len(CRUD_TWINS)


async def test_each_twin_copies_its_crud_siblings_gate_as_it_stands(sp):
    """An operator who relaxed or retuned the gate on an operation keeps that choice on every door to it: the gate of creating agents is one gate."""
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    relaxed = await store.get(crud_policy_id("update_agent"))
    await store.update(relaxed.model_copy(update={"enabled": False}))
    retuned = await store.get(crud_policy_id("create_trigger"))
    await store.update(retuned.model_copy(update={
        "approval": PolicyApprovalConfig(policy=_REGO), "timeout_seconds": 120.0,
        "approvers": ApproverSpec(kind="roles", roles=["admin"]), "preview_args": ["slug", "config.kind"],
    }))

    await M013ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    twin = rows[default_policy_id("system", "update_agent")]
    assert twin.enabled is False and twin.approval.type.value == "required"
    trigger_twin = rows[default_policy_id("trigger", "create")]
    assert trigger_twin.enabled is True and trigger_twin.approval.type.value == "policy" and trigger_twin.approval.policy == _REGO
    assert trigger_twin.timeout_seconds == 120.0 and trigger_twin.approvers == ApproverSpec(kind="roles", roles=["admin"])
    assert trigger_twin.preview_args == ["slug", "config.kind"]
    assert rows[default_policy_id("system", "create_agent")].enabled is True, "an untouched sibling gives the shipped gate"


async def test_a_delete_and_a_twin_whose_sibling_is_gone_get_the_shipped_gate(sp):
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    await store.delete(crud_policy_id("update_graph"))

    await M013ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    for toolset_id, tool_name in (("system", "delete_agent"), ("system", "delete_graph"), ("trigger", "delete"), ("system", "update_graph")):
        row = rows[default_policy_id(toolset_id, tool_name)]
        assert row.enabled is True and row.approval.type.value == "required", (toolset_id, tool_name)


async def test_a_sibling_under_an_operators_own_id_is_the_one_copied(sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="ops-agents", toolset_id="crud", tool_name="create_agent", enabled=False, approval=RequiredApprovalConfig()))

    await M013ApprovalTwinPolicies().apply(sp)

    assert (await store.get(default_policy_id("system", "create_agent"))).enabled is False


async def test_a_twin_that_already_has_a_policy_is_left_alone(sp):
    """A row the operator wrote for a twin, under any id or the retired spelling of its toolset, is its gate: no second row is written beside it."""
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="mine", toolset_id="_system", tool_name="update_agent", approval=PolicyApprovalConfig(policy=_REGO)))

    await M013ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    assert default_policy_id("system", "update_agent") not in rows
    assert rows["mine"].approval.type.value == "policy"


async def test_a_second_run_writes_nothing(sp):
    await _old_seed(sp)
    await M013ApprovalTwinPolicies().apply(sp)
    first = await _rows(sp)

    await M013ApprovalTwinPolicies().apply(sp)

    assert await _rows(sp) == first
