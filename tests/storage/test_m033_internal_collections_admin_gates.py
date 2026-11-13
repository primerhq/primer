"""Migration 33: the internal-collections writes get the shipped admin-tier gate on an upgraded install (Tools Universal M4).

Run on SQLite through the live policy model, so a row the migration writes is read back the way the gate reads it. These tests pin WHAT the migration
creates and re-stamps and what it leaves; the runner is pinned by ``tests/storage/test_migrations.py`` and the gates themselves, through every door and
for an admin-role run, by ``tests/toolset/test_admin_run_gates.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.bootstrap.seed import default_policy_id
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m033_internal_collections_admin_gates import M033InternalCollectionsAdminGates
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.admin_gates import INTERNAL_COLLECTIONS_WRITES

_REGO = "package primer.tool_approval\ndefault required := false\n"
_TOOLS = ("put_config", "delete_config", "bootstrap")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m033.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _rows(sp) -> dict[str, ToolApprovalPolicy]:
    page = await sp.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=100))
    return {row.id: row for row in page.items}


def _seeded_row(tool_name: str) -> ToolApprovalPolicy:
    """The row the generated-mutator seed wrote before the writes joined the admin-tier table (required gate, no approver spec)."""
    return ToolApprovalPolicy(
        id=default_policy_id("internal_collections", tool_name),
        toolset_id="internal_collections",
        tool_name=tool_name,
        enabled=True,
        approval=RequiredApprovalConfig(),
    )


def test_it_is_registered_as_version_33():
    assert any(isinstance(m, M033InternalCollectionsAdminGates) and m.version == 33 for m in MIGRATIONS)


async def test_an_install_without_rows_gets_the_shipped_gate_on_every_write(sp):
    await M033InternalCollectionsAdminGates().apply(sp)

    rows = await _rows(sp)
    assert set(rows) == {default_policy_id("internal_collections", tool) for tool in _TOOLS}
    for policy_id, row in rows.items():
        assert row.toolset_id == "internal_collections"
        assert (row.enabled, row.approval) == (True, RequiredApprovalConfig())
        assert row.approvers == ApproverSpec.model_validate(ADMIN_ONLY_SPEC), "an admin-tier gate is decided by an admin only"
        assert row.timeout_seconds is None and row.preview_args is None
        assert policy_id == default_policy_id("internal_collections", row.tool_name)


async def test_a_pre_admin_tier_seeded_row_is_re_stamped(sp):
    """A row still exactly as the generated-mutator seed wrote it (required gate, no approver spec) gets the admin-only approvers, nothing else."""
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_seeded_row("put_config"))

    await M033InternalCollectionsAdminGates().apply(sp)

    rows = await _rows(sp)
    row = rows[default_policy_id("internal_collections", "put_config")]
    assert row.approvers == ApproverSpec.model_validate(ADMIN_ONLY_SPEC), "re-stamped to the admin-only approvers"
    assert (row.enabled, row.approval) == (True, RequiredApprovalConfig()), "the rest of the row is untouched"
    assert row.timeout_seconds is None and row.preview_args is None
    # The other two writes get their shipped defaults.
    assert set(rows) == {default_policy_id("internal_collections", tool) for tool in _TOOLS}


async def test_a_row_the_operator_changed_is_left_alone(sp):
    """Every change a person makes to the gate is the operator's gate: the migration re-stamps nothing."""
    store = sp.get_storage(ToolApprovalPolicy)
    disabled = _seeded_row("put_config")
    disabled.enabled = False
    await store.create(disabled)
    retuned = _seeded_row("delete_config")
    retuned.timeout_seconds = 60.0
    await store.create(retuned)
    named = _seeded_row("bootstrap")
    named.approvers = ApproverSpec.model_validate({"kind": "users", "roles": [], "users": ["alice"]})
    await store.create(named)
    before = {pid: row.model_dump() for pid, row in (await _rows(sp)).items()}

    await M033InternalCollectionsAdminGates().apply(sp)

    rows = await _rows(sp)
    assert {pid: rows[pid].model_dump() for pid in before} == before


async def test_an_operator_row_under_another_id_gets_no_second_row(sp):
    """A policy the operator wrote under another id is the tool's gate: no create beside it, and the operator's row is kept as it is."""
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(ToolApprovalPolicy(id="mine-ic", toolset_id="internal_collections", tool_name="put_config", enabled=False,
                                          approval=RequiredApprovalConfig()))
    before = {pid: row.model_dump() for pid, row in (await _rows(sp)).items()}

    await M033InternalCollectionsAdminGates().apply(sp)

    rows = await _rows(sp)
    assert default_policy_id("internal_collections", "put_config") not in rows
    assert {pid: rows[pid].model_dump() for pid in before} == before
    # The other two writes still get their shipped defaults.
    assert {default_policy_id("internal_collections", "delete_config"), default_policy_id("internal_collections", "bootstrap")} <= set(rows)


async def test_a_second_run_writes_nothing(sp):
    await M033InternalCollectionsAdminGates().apply(sp)
    first = await _rows(sp)

    await M033InternalCollectionsAdminGates().apply(sp)

    assert await _rows(sp) == first


def test_the_frozen_copies_are_the_live_ones_today():
    """The step carries its own copies of the seeded shape, the approver spec and the policy ids (a later change to the live code must not change what
    it did); when it shipped they agreed with the live ones."""
    from primer.storage.migrations import m033_internal_collections_admin_gates as m033

    assert m033._ADMIN_ONLY == ADMIN_ONLY_SPEC
    for tool_name in _TOOLS:
        assert m033._policy_id(tool_name) == default_policy_id("internal_collections", tool_name)
        # The frozen seeded shape is what the pre-admin-tier seed wrote, through the live model.
        seeded = _seeded_row(tool_name).model_dump(mode="json")
        assert {key: value for key, value in m033._seeded(tool_name).items()} == {
            key: seeded.get(key) for key in m033._seeded(tool_name)
        }
    # The step's frozen tool list is the live admin-tier table's internal-collections keys.
    assert set(_TOOLS) == {tool for (toolset_id, tool) in INTERNAL_COLLECTIONS_WRITES if toolset_id == "internal_collections"}
