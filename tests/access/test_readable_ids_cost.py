"""What finding the readable ids costs the store (perf plan Task 4, stage A). Count-based; the answers themselves are pinned by the matrix.

The walk used to page whole rows 200 at a time (each page an OFFSET select plus a COUNT, each row fully decoded) only to read ``id`` and ``access``.
``Storage.find_ids`` returns just those two, keyset-paged, with no COUNT.
"""
from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.access.policy import readable_ids
from primer.access.workspaces import readable_workspace_ids
from primer.model.access import Access
from primer.model.workspace import Workspace
from tests.access.test_readable_ids_matrix import ME, PEOPLE, _provider, _seed
from tests.perf.counting import CountingStorageProvider


@pytest.mark.asyncio
async def test_450_visible_workspaces_are_found_with_one_ids_only_call_and_no_full_row_find():
    sp = CountingStorageProvider(await _provider("memory", None))
    await _seed(sp, 450)
    with sp.counted() as c:
        ids = await readable_workspace_ids(sp, PEOPLE["me"])
    assert len(ids) == 450
    assert c.calls("find", "list", model="Workspace") == 0, "no full-row page was read"
    assert c.calls("find_ids", model="Workspace") == 1
    assert c.total == 1


@pytest.mark.asyncio
async def test_the_sqlite_walk_is_one_select_without_a_count(tmp_path):
    sp = await _provider("sqlite", tmp_path)
    await _seed(sp, 201)
    statements: list[str] = []
    await sp.connection.set_trace_callback(statements.append)
    ids = await readable_workspace_ids(sp, PEOPLE["me"])
    await sp.connection.set_trace_callback(None)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(ids) == 201
    assert len(selects) == 1, selects
    assert "count(" not in selects[0].lower()
    await sp.aclose()


@pytest.mark.asyncio
async def test_admin_still_costs_nothing():
    sp = CountingStorageProvider(await _provider("memory", None))
    await _seed(sp, 10)
    with sp.counted() as c:
        assert await readable_ids(sp.get_storage(Workspace), Person("a", "admin")) is None
    assert c.total == 0


@pytest.mark.asyncio
async def test_a_row_without_an_access_block_grants_a_non_admin_nothing():
    sp = await _provider("memory", None)
    await _seed(sp, 3)
    store = sp.get_storage(Workspace)
    legacy = await store.get("ws-v0000")
    await store.update(legacy.model_copy(update={"access": Access()}))  # no owners, nothing shared
    got = await readable_workspace_ids(sp, Person(ME, "user"))
    assert "ws-v0000" not in got and len(got) == 2
