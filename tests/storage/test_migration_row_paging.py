"""M8: a migration reads each row once by keyset, with no COUNT per page and no OFFSET re-scan."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.provider import SqliteConfig
from primer.storage.migrations._rows import iter_rows, read_all
from primer.storage.migrations.m021_entity_ownership import M021EntityOwnership
from primer.storage.sqlite import SqliteStorageProvider

N = 450


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "rows.sqlite")))
    await provider.initialize()
    store = provider.get_storage(Agent)
    for i in range(N):
        await store.create(Agent(id=f"a{i:04d}", description="d", model=AgentModel(profile_id="p")))
    try:
        yield provider
    finally:
        await provider.aclose()


async def _trace(sp):
    statements: list[str] = []
    await sp.connection.set_trace_callback(statements.append)
    return statements


def _selects(statements):
    return [s for s in statements if s.lstrip().upper().startswith("SELECT")]


async def test_read_all_returns_every_row_once_in_id_order_with_no_counts(sp):
    statements = await _trace(sp)
    rows = await read_all(sp.get_storage(Agent))
    assert [r.id for r in rows] == [f"a{i:04d}" for i in range(N)]
    selects = _selects(statements)
    assert len(selects) == math.ceil(N / 200)
    assert not [s for s in selects if "count(" in s.lower() or "OFFSET" in s.upper()]


async def test_rows_updated_while_iterating_are_neither_skipped_nor_repeated(sp):
    store = sp.get_storage(Agent)
    seen = []
    async for row in iter_rows(store):
        seen.append(row.id)
        await store.update(row.model_copy(update={"description": "changed"}))
    assert seen == [f"a{i:04d}" for i in range(N)]
    assert all(r.description == "changed" for r in await read_all(store))


async def test_a_real_migration_over_450_rows_issues_no_counts(sp):
    statements = await _trace(sp)
    await M021EntityOwnership().apply(sp)
    assert not [s for s in _selects(statements) if "count(" in s.lower()]
