"""Migration 16: an upgraded install gets the shipped gates of the admin-tier writes (ticket 01a16a29).

Run on SQLite through the live policy model, so a row the migration writes is read back the way the gate reads it. These tests pin WHAT the
migration writes and leaves; the runner is pinned by ``tests/storage/test_migrations.py`` and the gates themselves, through every door and for an
admin-role run, by ``tests/toolset/test_admin_run_gates.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.bootstrap.seed import default_policy_id
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m016_admin_run_gates import M016AdminRunGates
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.admin_gates import ADMIN_GATES, HOST_CODE_TOOLSET_POLICY, HOST_CODE_TOOLSET_WRITES

_REGO = "package primer.tool_approval\ndefault required := false\n"


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m016.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _rows(sp) -> dict[str, ToolApprovalPolicy]:
    page = await sp.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=100))
    return {row.id: row for row in page.items}


def test_it_is_registered_as_version_16():
    assert any(isinstance(m, M016AdminRunGates) and m.version == 16 for m in MIGRATIONS)


async def test_an_untouched_install_gets_the_shipped_gate_on_every_admin_tier_write(sp):
    await M016AdminRunGates().apply(sp)

    rows = await _rows(sp)
    assert set(rows) == {default_policy_id(t, n) for t, n in ADMIN_GATES}
    for (toolset_id, tool_name), shipped in ADMIN_GATES.items():
        row = rows[default_policy_id(toolset_id, tool_name)]
        assert (row.toolset_id, row.tool_name, row.enabled) == (toolset_id, tool_name, True)
        assert row.approval == shipped, (toolset_id, tool_name)
        assert row.approvers is None and row.timeout_seconds is None and row.preview_args is None
    assert {rows[default_policy_id(t, n)].approval.type.value for t, n in HOST_CODE_TOOLSET_WRITES} == {"policy"}


@pytest.mark.parametrize("spelling", ["system", "_system"])
async def test_a_tool_that_already_has_a_policy_is_left_alone(sp, spelling):
    """A row the operator wrote, under any id, either spelling of the system toolset, enabled or not, is the tool's gate: no second row is written
    beside it, and the operator's row is kept as it is."""
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="mine-policies", toolset_id=spelling, tool_name="delete_tool_approval_policy", enabled=False,
                                          approval=RequiredApprovalConfig()))
    await store.create(ToolApprovalPolicy(id="mine-toolsets", toolset_id=spelling, tool_name="create_toolset", approval=PolicyApprovalConfig(policy=_REGO)))
    await store.create(ToolApprovalPolicy(id="mine-harness", toolset_id="harness", tool_name="harness__install", enabled=False,
                                          approval=RequiredApprovalConfig()))
    before = {pid: row.model_dump() for pid, row in (await _rows(sp)).items()}

    await M016AdminRunGates().apply(sp)

    rows = await _rows(sp)
    for toolset_id, tool_name in (("system", "delete_tool_approval_policy"), ("system", "create_toolset"), ("harness", "harness__install")):
        assert default_policy_id(toolset_id, tool_name) not in rows
    assert {pid: rows[pid].model_dump() for pid in before} == before
    assert default_policy_id("system", "update_toolset") in rows, "the other tools are still gated"


async def test_a_seeded_row_already_there_is_not_rewritten(sp):
    """A boot that crashed after the ensure pass seeded a row (or an operator who retuned it) keeps it as it is."""
    store = sp.get_storage(ToolApprovalPolicy)
    pid = default_policy_id("harness", "harness__sync")
    await store.create(ToolApprovalPolicy(id=pid, toolset_id="harness", tool_name="harness__sync", enabled=False, timeout_seconds=60.0,
                                          approval=RequiredApprovalConfig()))

    await M016AdminRunGates().apply(sp)

    row = (await _rows(sp))[pid]
    assert (row.enabled, row.timeout_seconds) == (False, 60.0)


async def test_a_second_run_writes_nothing(sp):
    await M016AdminRunGates().apply(sp)
    first = await _rows(sp)

    await M016AdminRunGates().apply(sp)

    assert await _rows(sp) == first


def test_the_frozen_table_is_the_live_one_today():
    """The step carries its own copy of the table, the Rego text and the policy ids (a later change to the live code must not change what it did);
    when it shipped they agreed with the live ones."""
    from primer.storage.migrations import m016_admin_run_gates as m016

    frozen = {(t, n): approval for t, n, approval in m016._GATES}
    assert set(frozen) == set(ADMIN_GATES)
    assert all(frozen[key] == shipped.model_dump(mode="json") for key, shipped in ADMIN_GATES.items())
    assert m016._HOST_CODE_TOOLSET_POLICY == HOST_CODE_TOOLSET_POLICY
    assert all(m016._policy_id(t, n) == default_policy_id(t, n) for t, n, _ in m016._GATES)
