"""Migration 20: an upgraded install's harness lifecycle gates move from the scoped tool name to the bare one.

Run on SQLite through the live policy model, so a row the migration moves is read back the way the gate reads it. These
tests pin WHAT the migration moves and leaves; the runner is pinned by ``tests/storage/test_migrations.py`` and the gates
themselves, through every door, by ``tests/toolset/test_admin_run_gates.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m020_harness_policy_rekey import M020HarnessPolicyRekey
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml, so async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m020.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _rows(sp) -> dict[str, ToolApprovalPolicy]:
    page = await sp.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=100))
    return {row.id: row for row in page.items}


def _old_id(name: str) -> str:
    """The id the ensure pass and migration 16 gave a policy stored under the scoped name."""
    return f"tool-approval-policy-harness-{name.replace('_', '-')}"


def test_it_is_registered_as_version_20() -> None:
    assert any(isinstance(m, M020HarnessPolicyRekey) and m.version == 20 for m in MIGRATIONS)


async def test_a_seeded_row_moves_to_the_bare_key_with_everything_else_kept(sp):
    """The step moves the key, not the gate: the row's id and settings survive, so approval records and operator tuning keep working."""
    store = sp.get_storage(ToolApprovalPolicy)
    pid = _old_id("harness__sync")
    await store.create(
        ToolApprovalPolicy(
            id=pid, toolset_id="harness", tool_name="harness__sync", enabled=False, timeout_seconds=60.0,
            approval=RequiredApprovalConfig(),
        ),
    )

    await M020HarnessPolicyRekey().apply(sp)

    rows = await _rows(sp)
    assert set(rows) == {pid}, "the row keeps its id; a new id would dangle the approval records that reference it"
    row = rows[pid]
    assert (row.toolset_id, row.tool_name, row.enabled, row.timeout_seconds) == ("harness", "sync", False, 60.0)


async def test_an_install_that_never_ran_m016_gets_nothing(sp):
    """A fresh install baselines past the migration; the ensure pass seeds the gates under the bare keys from the start."""
    await M020HarnessPolicyRekey().apply(sp)

    assert await _rows(sp) == {}


async def test_a_row_the_operator_wrote_at_the_new_key_wins(sp):
    """A gate already stored under the live key is the operator's: it is kept, and its old-key twin is left behind rather than
    duplicated beside it (a second row would be read as unconditional, decided by an admin only)."""
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(
        ToolApprovalPolicy(
            id=_old_id("harness__install"), toolset_id="harness", tool_name="harness__install", enabled=True,
            approval=RequiredApprovalConfig(),
        ),
    )
    await store.create(
        ToolApprovalPolicy(
            id="mine", toolset_id="harness", tool_name="install", enabled=False,
            approval=RequiredApprovalConfig(),
        ),
    )
    before = {pid: row.model_dump() for pid, row in (await _rows(sp)).items()}

    await M020HarnessPolicyRekey().apply(sp)

    assert {pid: rows.model_dump() for pid, rows in (await _rows(sp)).items()} == before


async def test_a_second_run_writes_nothing(sp):
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(
        ToolApprovalPolicy(
            id=_old_id("harness__uninstall"), toolset_id="harness", tool_name="harness__uninstall", enabled=True,
            approval=RequiredApprovalConfig(),
        ),
    )

    await M020HarnessPolicyRekey().apply(sp)
    first = await _rows(sp)

    await M020HarnessPolicyRekey().apply(sp)

    assert await _rows(sp) == first
