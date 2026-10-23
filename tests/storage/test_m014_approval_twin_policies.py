"""Migration 14: an upgraded install gates every twin of a crud tool as its crud sibling is gated NOW (audit G6-1).

Run on SQLite through the live policy model, so a row the migration writes is read back the way the gate reads it. These tests pin WHAT the
migration writes and leaves; the runner is pinned by ``tests/storage/test_migrations.py`` and the gates themselves, through every door, by
``tests/toolset/test_approval_twins.py``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.bootstrap.seed import crud_policy_id, default_policy_id
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ApproverSpec, LlmApprovalConfig, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m014_approval_twin_policies import M014ApprovalTwinPolicies
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS

_REGO = "package primer.tool_approval\ndefault required := false\nrequired if input.arguments.id == \"operator\"\n"


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m014.sqlite"))
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


def test_it_is_registered_as_version_14():
    assert any(isinstance(m, M014ApprovalTwinPolicies) and m.version == 14 for m in MIGRATIONS)


async def test_an_untouched_install_gets_the_shipped_gate_on_every_twin(sp):
    await _old_seed(sp)

    await M014ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    for toolset_id, tool_name in CRUD_TWINS:
        row = rows[default_policy_id(toolset_id, tool_name)]
        assert (row.toolset_id, row.tool_name, row.enabled, row.approval.type.value) == (toolset_id, tool_name, True, "required")
        assert row.approvers is None and row.timeout_seconds is None and row.preview_args is None
    assert len(rows) == len(CRUD_TOOL_NAMES) + len(CRUD_TWINS)


async def test_each_twin_copies_its_crud_siblings_plain_gate_as_it_stands(sp):
    """An operator who relaxed or retuned a plain ``required`` gate keeps that choice on every door to the operation: the gate of creating agents is
    one gate."""
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    relaxed = await store.get(crud_policy_id("update_agent"))
    await store.update(relaxed.model_copy(update={"enabled": False}))
    retuned = await store.get(crud_policy_id("create_trigger"))
    await store.update(retuned.model_copy(update={
        "timeout_seconds": 120.0, "approvers": ApproverSpec(kind="roles", roles=["admin"]), "preview_args": ["slug", "config.kind"],
    }))

    await M014ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    twin = rows[default_policy_id("system", "update_agent")]
    assert twin.enabled is False and twin.approval.type.value == "required"
    trigger_twin = rows[default_policy_id("trigger", "create")]
    assert trigger_twin.enabled is True and trigger_twin.approval.type.value == "required"
    assert trigger_twin.timeout_seconds == 120.0 and trigger_twin.approvers == ApproverSpec(kind="roles", roles=["admin"])
    assert trigger_twin.preview_args == ["slug", "config.kind"]
    assert rows[default_policy_id("system", "create_agent")].enabled is True, "an untouched sibling gives the shipped gate"


@pytest.mark.parametrize("approval", [
    PolicyApprovalConfig(policy=_REGO),
    LlmApprovalConfig(provider_id="anthropic-1", model="claude-x", prompt="Is this create safe?"),
], ids=["rego", "llm-judge"])
async def test_a_conditional_sibling_gate_is_not_copied_and_the_twin_gets_the_shipped_gate(sp, caplog, approval):
    """A Rego or LLM-judge condition was written for the crud call: one that reads ``input.tool_name`` (``create_agent``) or ``input.toolset_id``
    (``crud``) never fires on ``system::create_agent``, so a copy could leave the twin ungated in effect. The twin gets the shipped ``required`` gate,
    and a WARNING names both policies."""
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    sibling = await store.get(crud_policy_id("create_agent"))
    await store.update(sibling.model_copy(update={"approval": approval, "timeout_seconds": 30.0}))

    with caplog.at_level(logging.WARNING, logger="primer.storage.migrations.m014_approval_twin_policies"):
        await M014ApprovalTwinPolicies().apply(sp)

    twin = (await _rows(sp))[default_policy_id("system", "create_agent")]
    assert (twin.enabled, twin.approval.type.value, twin.timeout_seconds) == (True, "required", None)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1, warned
    assert crud_policy_id("create_agent") in warned[0] and default_policy_id("system", "create_agent") in warned[0]


def test_the_frozen_twin_table_is_the_live_one_today():
    """The step carries its own copy of the twin table and the policy ids (a later change to the live code must not change what it did); when it
    shipped they agreed with the live ones."""
    from primer.storage.migrations import m014_approval_twin_policies as m014

    assert {(t, n): s for t, n, s in m014._TWINS} == dict(CRUD_TWINS)
    assert all(m014._policy_id(t, n) == default_policy_id(t, n) for t, n, _ in m014._TWINS)
    assert m014._policy_id("crud", "create_agent") == crud_policy_id("create_agent")


async def test_a_delete_and_a_twin_whose_sibling_is_gone_get_the_shipped_gate(sp):
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    await store.delete(crud_policy_id("update_graph"))

    await M014ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    for toolset_id, tool_name in (("system", "delete_agent"), ("system", "delete_graph"), ("trigger", "delete"), ("system", "update_graph")):
        row = rows[default_policy_id(toolset_id, tool_name)]
        assert row.enabled is True and row.approval.type.value == "required", (toolset_id, tool_name)


async def test_a_sibling_under_an_operators_own_id_is_the_one_copied(sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="ops-agents", toolset_id="crud", tool_name="create_agent", enabled=False, approval=RequiredApprovalConfig()))

    await M014ApprovalTwinPolicies().apply(sp)

    assert (await store.get(default_policy_id("system", "create_agent"))).enabled is False


async def test_a_twin_that_already_has_a_policy_is_left_alone(sp):
    """A row the operator wrote for a twin, under any id or the retired spelling of its toolset, is its gate: no second row is written beside it."""
    await _old_seed(sp)
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="mine", toolset_id="_system", tool_name="update_agent", approval=PolicyApprovalConfig(policy=_REGO)))

    await M014ApprovalTwinPolicies().apply(sp)

    rows = await _rows(sp)
    assert default_policy_id("system", "update_agent") not in rows
    assert rows["mine"].approval.type.value == "policy"


async def test_a_second_run_writes_nothing(sp):
    await _old_seed(sp)
    await M014ApprovalTwinPolicies().apply(sp)
    first = await _rows(sp)

    await M014ApprovalTwinPolicies().apply(sp)

    assert await _rows(sp) == first
