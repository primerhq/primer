"""M1 / L1: a page costs a COUNT(*) only when its caller reads ``total`` and the page does not already show the end of the result."""

from __future__ import annotations

import math

import pytest

from primer.model.collection import Collection
from primer.model.storage import FieldRef, OffsetPage, Op, OrderBy, Predicate, Value
from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider


async def _provider(tmp_path, n: int = 450):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "c.sqlite")))
    await sp.initialize()
    store = sp.get_storage(Collection)
    for i in range(n):
        await store.create(Collection(id=f"c-{i:04d}", description="even" if i % 2 == 0 else "odd"))
    statements: list[str] = []
    await sp.connection.set_trace_callback(statements.append)  # every executed SQL statement
    return sp, store, statements


def _counts(statements):
    return [s for s in statements if "count(*)" in s.lower() and "FROM \"collection\"" in s]


def _selects(statements):
    return [s for s in statements if s.lstrip().upper().startswith("SELECT ID, DATA")]


@pytest.mark.asyncio
async def test_pages_and_totals_are_what_they_were(tmp_path):
    sp, store, _ = await _provider(tmp_path, 450)
    first = await store.list(OffsetPage(offset=0, length=200))
    assert (first.offset, first.length, first.total) == (0, 200, 450)
    last = await store.list(OffsetPage(offset=400, length=200))
    assert (last.length, last.total) == (50, 450)
    past = await store.list(OffsetPage(offset=900, length=200))
    assert (past.length, past.total) == (0, 450)  # a page past the end still reports the real total
    evens = Predicate(left=FieldRef(name="description"), op=Op.EQ, right=Value(value="even"))
    filtered = await store.find(evens, OffsetPage(offset=0, length=10), order_by=[OrderBy(field="id", direction="desc")])
    assert (filtered.length, filtered.total) == (10, 225)
    assert filtered.items[0].id == "c-0448"
    short = await store.find(evens, OffsetPage(offset=220, length=200))
    assert (short.length, short.total) == (5, 225)
    await sp.aclose()


@pytest.mark.asyncio
async def test_a_scan_that_ignores_total_issues_no_counts(tmp_path):
    sp, store, statements = await _provider(tmp_path, 450)
    seen, offset = [], 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=200, want_total=False))
        seen += [c.id for c in page.items]
        if len(page.items) < 200:
            break
        offset += 200
    assert len(seen) == 450 and len(set(seen)) == 450
    assert len(_selects(statements)) == math.ceil(450 / 200)
    assert _counts(statements) == []
    assert page.total == 450  # the last page is short: its total is free
    await sp.aclose()


@pytest.mark.asyncio
async def test_a_full_page_that_wants_its_total_still_counts_once_and_a_short_page_never_does(tmp_path):
    sp, store, statements = await _provider(tmp_path, 450)
    await store.list(OffsetPage(offset=0, length=200))
    assert len(_counts(statements)) == 1
    statements.clear()
    page = await store.list(OffsetPage(offset=400, length=200))
    assert page.total == 450 and _counts(statements) == []
    await sp.aclose()


@pytest.mark.asyncio
async def test_the_default_page_keeps_total_in_the_rest_envelope():
    assert OffsetPage(offset=0, length=20).want_total is True


@pytest.mark.asyncio
async def test_count_by_groups_in_one_query_and_matches_a_walk(tmp_path):
    from datetime import datetime, timezone

    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "g.sqlite")))
    await sp.initialize()
    store = sp.get_storage(WorkspaceSession)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    layout = {"w1": 3, "w2": 450, "w3": 1}
    for ws, n in layout.items():
        for i in range(n):
            await store.create(WorkspaceSession(
                id=f"{ws}-{i}", workspace_id=ws, binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING, created_at=now,
            ))
    statements: list[str] = []
    await sp.connection.set_trace_callback(statements.append)

    counts = await store.count_by("workspace_id", ["w1", "w2", "w3", "w-none"])

    assert counts == layout  # a workspace with no sessions is absent, as the old walk's Counter had it
    queries = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(queries) == 1 and "group by" in queries[0].lower(), statements
    assert await store.count_by("workspace_id", []) == {}
    await sp.aclose()
