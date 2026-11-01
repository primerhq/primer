"""PERF-T16 (M8): a converted migration reads only the rows it changes (a predicate, never the whole table) and writes each with ``patch_if``.

5000 rows are seeded per table, a few of which need the change: the step must read and write O(those rows), not O(table).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from primer.model.provider import SqliteConfig
from primer.storage.migrations.m024_cross_workspace_list_field import (
    ExternalToolCall as CallView,
    M024CrossWorkspaceListField,
    WorkspaceSession as SessionView,
)
from primer.storage.sqlite import SqliteStorageProvider
from tests.perf.counting import CountingStorageProvider
from tests.storage._migration_golden import insert_raw

N = 5000
NEED = 50


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[CountingStorageProvider]:
    inner = SqliteStorageProvider(SqliteConfig(path=tmp_path / "perf.sqlite"))
    await inner.initialize()
    try:
        yield CountingStorageProvider(inner)
    finally:
        await inner.aclose()


def _mix(needs: dict, done: dict) -> dict[str, dict]:
    """``N`` documents, every ``N // NEED``-th of which still needs the change."""
    return {f"r{i:05d}": dict(needs if i % (N // NEED) == 0 else done) for i in range(N)}


async def test_m024_reads_and_writes_only_the_rows_without_a_workspace(sp) -> None:
    await insert_raw(sp, SessionView, {"s1": {"workspace_id": "ws-1"}})
    await insert_raw(sp, CallView, _mix({"session_id": "s1"}, {"session_id": "s1", "workspace_id": "ws-1"}))

    with sp.counted() as c:
        await M024CrossWorkspaceListField().apply(sp)

    assert c.calls("list") == 0, "a full-table read"
    assert c.calls("update") == 0
    assert c.calls("patch_if", model="ExternalToolCall") == NEED
    assert c.calls("find", model="ExternalToolCall") == 1
    assert c.rows <= 2 * NEED + 1  # each changed row read once and written once, plus the one session
    assert c.total <= NEED + 3  # the writes, one page per table, one session read
