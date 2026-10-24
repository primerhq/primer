"""Migration 18: an upgraded install loses the shipped gate on ``crud__list_python_tools``, a read (ticket 01a16f1d ruling 4).

The ensure pass gated every crud tool, the read-only ``list_python_tools`` included; it now gates only the writes. A fresh install no longer gets the
row; this step removes it from an install that has it, but only as it was seeded (enabled, ``required``, no timeout, no approvers, no preview paths): a
row the operator changed, or wrote under another id, is the operator's gate and stays. Run on SQLite through the live policy model, as migration 16's
tests are; ``tests/toolset/test_approval_twins.py`` checks that an upgraded install then holds exactly a fresh install's gates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.bootstrap.seed import crud_policy_id
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ApproverSpec, PolicyApprovalConfig, RequiredApprovalConfig, ToolApprovalPolicy
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m018_ungate_list_python_tools import M018UngateListPythonTools
from primer.storage.sqlite import SqliteStorageProvider

SEEDED = crud_policy_id("list_python_tools")
SIBLING = crud_policy_id("create_python_toolset")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m018.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _seeded(**over) -> ToolApprovalPolicy:
    """The row the ensure pass wrote before this step."""
    return ToolApprovalPolicy(**{"id": SEEDED, "toolset_id": "crud", "tool_name": "list_python_tools", "approval": RequiredApprovalConfig(), **over})


async def _ids(sp) -> set[str]:
    page = await sp.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=100))
    return {row.id for row in page.items}


def test_it_is_registered_as_version_18():
    assert any(isinstance(m, M018UngateListPythonTools) and m.version == 18 for m in MIGRATIONS)


async def test_the_gate_as_seeded_is_removed_and_the_writes_keep_theirs(sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_seeded())
    await store.create(ToolApprovalPolicy(id=SIBLING, toolset_id="crud", tool_name="create_python_toolset", approval=RequiredApprovalConfig()))

    await M018UngateListPythonTools().apply(sp)

    assert await _ids(sp) == {SIBLING}


@pytest.mark.parametrize("change", [
    {"enabled": False},
    {"timeout_seconds": 60.0},
    {"approvers": ApproverSpec.model_validate(ADMIN_ONLY_SPEC)},
    {"preview_args": ["toolset_id"]},
    {"approval": PolicyApprovalConfig(policy="package primer.tool_approval\ndefault required := true\n")},
], ids=["disabled", "timeout", "approvers", "preview", "rego"])
async def test_a_gate_the_operator_changed_is_left_alone(sp, change):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_seeded(**change))
    before = (await store.get(SEEDED)).model_dump()

    await M018UngateListPythonTools().apply(sp)

    assert (await store.get(SEEDED)).model_dump() == before


async def test_an_operators_own_policy_under_another_id_is_left_alone(sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_seeded(id="mine"))

    await M018UngateListPythonTools().apply(sp)

    assert await _ids(sp) == {"mine"}


async def test_an_install_without_the_row_and_a_second_run_change_nothing(sp):
    await M018UngateListPythonTools().apply(sp)
    assert await _ids(sp) == set()

    await sp.get_storage(ToolApprovalPolicy).create(_seeded())
    await M018UngateListPythonTools().apply(sp)
    await M018UngateListPythonTools().apply(sp)
    assert await _ids(sp) == set()
