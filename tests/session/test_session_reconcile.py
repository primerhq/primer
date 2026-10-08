"""Tests for primer.workspace.session_reconcile.reconcile_sessions_to_workspace_lost.

No prior test file exercised this function at all. Adding coverage now
because 01a04d91-a7a0 changed its behavior: a session reconciled to
ENDED/workspace_lost must also have turn_status/turn_started_at reset,
since a workspace confirmed permanently unreachable is exactly the crash
scenario those fields exist to catch (the worker that was mid-turn on it
is gone and never reached its own cleanup).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider
    return _FakeStorageProvider()


@pytest.mark.asyncio
async def test_reconcile_clears_stale_running_turn_status(
    fake_storage_provider,
) -> None:
    """A session stuck at turn_status='running' (its worker died along
    with the workspace, so run_one_session_turn's own finally/except
    cleanup never ran) must be reset to idle by reconciliation, not left
    ENDED with a permanently-stuck 'running' turn_status."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    started = _now()
    await storage.create(WorkspaceSession(
        id="s-lost",
        workspace_id="w-gone",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=started,
        turn_status="running",
        turn_started_at=started,
    ))

    reconciled = await reconcile_sessions_to_workspace_lost(
        fake_storage_provider, "w-gone",
    )

    assert reconciled == 1
    row = await storage.get("s-lost")
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "workspace_lost"
    assert row.turn_status == "idle"
    assert row.turn_started_at is None


@pytest.mark.asyncio
async def test_reconcile_skips_already_ended_sessions(
    fake_storage_provider,
) -> None:
    """An already-ENDED session on the lost workspace is left untouched -
    reconciliation must not overwrite a real ended_reason/turn_status a
    prior clean exit already wrote."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(WorkspaceSession(
        id="s-done",
        workspace_id="w-gone",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.ENDED,
        ended_reason="completed",
        created_at=_now(),
        turn_status="idle",
    ))

    reconciled = await reconcile_sessions_to_workspace_lost(
        fake_storage_provider, "w-gone",
    )

    assert reconciled == 0
    row = await storage.get("s-done")
    assert row.ended_reason == "completed"


# ---- every session of the workspace, not the first page of them (ticket 01a11b93, found in the #541 review) -------------------------------------------------------


def _row(session_id: str, workspace_id: str, status: SessionStatus) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason="completed" if status == SessionStatus.ENDED else None,
        created_at=_now(),
        turn_status="running" if status == SessionStatus.RUNNING else "idle",
    )


async def _statuses(storage, workspace_id: str) -> dict[str, tuple[SessionStatus, str | None]]:
    from primer.model.storage import CursorPage

    rows = []
    cursor = None
    while True:
        page = await storage.list(CursorPage(cursor=cursor, length=200))
        rows.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return {r.id: (r.status, r.ended_reason) for r in rows if r.workspace_id == workspace_id}


@pytest.mark.asyncio
async def test_open_sessions_beyond_the_first_page_are_ended_too(fake_storage_provider) -> None:
    """The function read ONE page of 200 rows. A workspace with more sessions than that, ENDED ones included, kept every open session past the first page
    running against a workspace that no longer exists (the row and the files are deleted by the destroy that called this)."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    # 250 ended sessions first (they sort first by id), then 230 open ones: a single page of 200 holds ended rows only.
    for i in range(250):
        await storage.create(_row(f"a-{i:04d}", "w-gone", SessionStatus.ENDED))
    for i in range(230):
        await storage.create(_row(f"b-{i:04d}", "w-gone", SessionStatus.RUNNING if i % 2 else SessionStatus.WAITING))

    reconciled = await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone")

    assert reconciled == 230
    after = await _statuses(storage, "w-gone")
    still_open = sorted(i for i, (status, _) in after.items() if status != SessionStatus.ENDED)
    assert still_open == [], f"{len(still_open)} open sessions were not ended, e.g. {still_open[:3]}"
    assert all(reason == "workspace_lost" for i, (status, reason) in after.items() if i.startswith("b-"))
    assert all(reason == "completed" for i, (status, reason) in after.items() if i.startswith("a-")), "an ended session's own reason was overwritten"


@pytest.mark.asyncio
async def test_more_than_one_page_of_open_sessions_are_all_ended(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    for i in range(450):
        await storage.create(_row(f"s-{i:04d}", "w-gone", SessionStatus.RUNNING))

    reconciled = await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone")

    assert reconciled == 450
    assert all(status == SessionStatus.ENDED for status, _ in (await _statuses(storage, "w-gone")).values())


@pytest.mark.asyncio
async def test_other_workspaces_are_left_alone(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    for i in range(210):
        await storage.create(_row(f"gone-{i:04d}", "w-gone", SessionStatus.RUNNING))
    for i in range(210):
        await storage.create(_row(f"kept-{i:04d}", "w-kept", SessionStatus.RUNNING))

    await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone")

    kept = await _statuses(storage, "w-kept")
    assert len(kept) == 210 and all(status == SessionStatus.RUNNING for status, _ in kept.values())


@pytest.mark.asyncio
async def test_one_session_that_cannot_be_updated_does_not_stop_the_rest(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    for i in range(205):
        await storage.create(_row(f"s-{i:04d}", "w-gone", SessionStatus.RUNNING))
    real_update = storage.update

    async def flaky_update(entity):
        if entity.id == "s-0003":
            raise RuntimeError("row is locked")
        return await real_update(entity)

    storage.update = flaky_update

    reconciled = await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone")

    assert reconciled == 204
    after = await _statuses(storage, "w-gone")
    assert after["s-0003"][0] == SessionStatus.RUNNING and after["s-0204"][0] == SessionStatus.ENDED


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_read_part_way_still_ends_what_was_read(fake_storage_provider) -> None:
    """Best-effort, as before: a failing query is logged and swallowed, and the sessions already read are not abandoned with it."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    # 250 ended sessions that sort first: the status filter must be in the QUERY, or the one page that is read holds ended rows only and nothing is reconciled
    # (a filter applied after the read is invisible to a test that has fewer ended rows than a page).
    for i in range(250):
        await storage.create(_row(f"a-{i:04d}", "w-gone", SessionStatus.ENDED))
    for i in range(450):
        await storage.create(_row(f"b-{i:04d}", "w-gone", SessionStatus.RUNNING))
    real_find = storage.find
    calls = {"n": 0}

    async def find_then_fail(predicate, page, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return await real_find(predicate, page, **kwargs)

    storage.find = find_then_fail

    reconciled = await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone")

    assert reconciled == 200, "the first page of OPEN sessions was read and must be reconciled even though the second could not be read"
    after = await _statuses(storage, "w-gone")
    assert sum(1 for i, (status, reason) in after.items() if i.startswith("b-") and reason == "workspace_lost") == 200
    assert all(reason == "completed" for i, (status, reason) in after.items() if i.startswith("a-")), "an ended session's own reason was overwritten"


@pytest.mark.asyncio
async def test_the_first_page_failing_reconciles_nothing_and_does_not_raise(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_row("s-1", "w-gone", SessionStatus.RUNNING))

    async def broken_find(predicate, page, **kwargs):
        raise RuntimeError("database went away")

    storage.find = broken_find

    assert await reconcile_sessions_to_workspace_lost(fake_storage_provider, "w-gone") == 0


@pytest.mark.asyncio
async def test_the_same_on_a_real_sqlite_store(tmp_path) -> None:
    """The fake pages by offset; the real backends page by key. Ending rows between pages must not move a key cursor, and the predicate (this workspace AND not
    ended) must run in the database, so the same shape as the first test is repeated on a real SQLite store."""
    from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(WorkspaceSession)
        for i in range(250):
            await storage.create(_row(f"a-{i:04d}", "w-gone", SessionStatus.ENDED))
        for i in range(450):
            await storage.create(_row(f"b-{i:04d}", "w-gone", SessionStatus.RUNNING))
        for i in range(30):
            await storage.create(_row(f"c-{i:04d}", "w-kept", SessionStatus.RUNNING))

        reconciled = await reconcile_sessions_to_workspace_lost(sp, "w-gone")

        assert reconciled == 450
        gone = await _statuses(storage, "w-gone")
        assert all(status == SessionStatus.ENDED for status, _ in gone.values())
        assert all(reason == "completed" for i, (_, reason) in gone.items() if i.startswith("a-"))
        kept = await _statuses(storage, "w-kept")
        assert len(kept) == 30 and all(status == SessionStatus.RUNNING for status, _ in kept.values())
    finally:
        await sp.aclose()
