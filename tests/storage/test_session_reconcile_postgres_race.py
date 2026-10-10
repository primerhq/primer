"""The workspace-lost reconcile against a writer that moves a session while its UPDATE waits, on a live Postgres (ticket 01a11d29, the race found in the review of #718).

The reconcile ends each open session by ONE fenced ``patch_if`` (``where status`` is not ended). Writer A moves the row inside an open transaction, so it holds the row lock; the reconcile's
UPDATE blocks on that lock; A commits. Under READ COMMITTED, Postgres then re-checks the reconcile's WHERE against A's committed version (EvalPlanQual). The fence is a multi-valued
guard, and while it was compiled as ``IN (SELECT jsonb_array_elements(..))`` that re-check reused the one element the first pass matched: a row A moved from ``running`` to ``waiting`` was
refused although it was not ended, and the reconcile read the refusal as "another path ended it" (uncounted, unlogged, never retried), leaving a session open on a dead workspace. #723
compiles the guard as ``= ANY(ARRAY(..))``, which re-checks the whole list. The in-memory fake and SQLite have no such re-check, so this runs on Postgres only (gate:
``PRIMER_TEST_POSTGRES_URL``, see ``tests/pg_gate.py``), with two real connections and a real row lock.

What it pins for THIS caller: an open-to-open move while the UPDATE waits still ends the session ``workspace_lost`` and counts it, and a field A committed alongside survives; a move to
ENDED keeps the other path's reason, is not counted, and is logged as left alone.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.workspace_session import (
    NON_ENDED_STATUSES,
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage import postgres as pg_backend
from primer.storage.postgres import PostgresStorageProvider
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the Postgres session reconcile race tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

WORKSPACE = "w-gone"
ROW = "s-race"
#: Every wait is bounded: a writer that never unblocks fails the test instead of hanging the lane.
_BOUND_S = 30.0


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[PostgresStorageProvider]:
    """A provider on a throwaway schema of the gated database, dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"rcr{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))
    async with asyncio.timeout(_BOUND_S):
        await sp.initialize()
    try:
        yield sp
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            # The "table ensured" cache is keyed by id(provider): a later provider at this address must not skip its CREATE.
            for key in [k for k in pg_backend._table_ensured if k[0] == id(sp)]:
                pg_backend._table_ensured.discard(key)
            await sp.aclose()


def _row(session_id: str, status: SessionStatus, workspace_id: str = WORKSPACE) -> WorkspaceSession:
    running = status == SessionStatus.RUNNING
    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason="completed" if status == SessionStatus.ENDED else None,
        created_at=datetime.now(timezone.utc),
        turn_status="running" if running else "idle",
        turn_started_at=datetime.now(timezone.utc) if running else None,
    )


async def _until_something_waits_on(provider: PostgresStorageProvider, blocker: Any, task: asyncio.Task[Any]) -> None:
    """Return once a backend waits on a lock ``blocker``'s backend holds. Fails if ``task`` ends first: the reconcile then never waited, and the re-check this file is about never
    ran. The caller bounds the wait."""
    blocker_pid = blocker.get_server_pid()
    async with provider.pool.acquire() as probe:
        while not task.done():
            if await probe.fetchval("SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))", blocker_pid):
                return
            await asyncio.sleep(0.02)
    raise AssertionError("the reconcile finished without waiting on A's row lock, so the re-check was never exercised")


async def _reconcile_blocked_on_a(provider: PostgresStorageProvider, a_patch: dict[str, Any], a_where: list[str]) -> int:
    """A writes ``a_patch`` to ``ROW`` and holds the row lock; the reconcile reads the committed row, then its UPDATE blocks on that lock; A commits; the reconcile's count is returned.
    On any failure A is rolled back (which frees the reconcile) and the reconcile is awaited. Every await is bounded, the connections' acquire and release included."""
    store = provider.get_storage(WorkspaceSession)
    async with asyncio.timeout(3 * _BOUND_S), provider.pool.acquire() as c1:
        tx1 = c1.transaction()
        await tx1.start()
        committed = False
        task: asyncio.Task[int] | None = None
        try:
            async with asyncio.timeout(_BOUND_S):
                assert await store.patch_if(ROW, a_patch, where={"status": a_where}, conn=c1) is not None
                task = asyncio.create_task(reconcile_sessions_to_workspace_lost(provider, WORKSPACE))
                await _until_something_waits_on(provider, c1, task)
                await tx1.commit()
                committed = True
                return await task
        finally:
            async with asyncio.timeout(_BOUND_S):
                if not committed:
                    await tx1.rollback()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)


_OPEN_TO_OPEN = [
    pytest.param(SessionStatus.RUNNING, "waiting", id="running-to-waiting"),
    pytest.param(SessionStatus.CREATED, "running", id="created-to-running"),
    pytest.param(SessionStatus.WAITING, "paused", id="waiting-to-paused"),
]


@pytest.mark.parametrize("start,moved_to", _OPEN_TO_OPEN)
async def test_a_session_moved_to_another_open_status_while_the_update_waits_is_still_ended_workspace_lost_and_counted(
    provider: PostgresStorageProvider, caplog: pytest.LogCaptureFixture, start: SessionStatus, moved_to: str,
) -> None:
    store = provider.get_storage(WorkspaceSession)
    async with asyncio.timeout(_BOUND_S):
        await store.create(_row(ROW, start))
        await store.create(_row("s-done", SessionStatus.ENDED))
        await store.create(_row("s-kept", SessionStatus.RUNNING, workspace_id="w-kept"))

    with caplog.at_level(logging.INFO, logger="primer.workspace.session_reconcile"):
        reconciled = await _reconcile_blocked_on_a(provider, {"status": moved_to, "last_seq": 41}, [start.value])

    async with asyncio.timeout(_BOUND_S):
        row, done, kept = await store.get(ROW), await store.get("s-done"), await store.get("s-kept")
    assert row is not None and done is not None and kept is not None
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "workspace_lost"), "a session that is not ended was left open on a dead workspace"
    assert reconciled == 1, "the session the reconcile ended was not counted"
    assert row.last_seq == 41, "a field the other writer committed was put back from the stale snapshot"
    assert row.turn_status == "idle" and row.turn_started_at is None
    assert done.ended_reason == "completed" and kept.status == SessionStatus.RUNNING
    assert not [r.getMessage() for r in caplog.records if r.name == "primer.workspace.session_reconcile" and r.levelno >= logging.WARNING], "the session was ended: nothing to warn about"


async def test_a_session_ended_by_another_path_while_the_update_waits_keeps_that_reason_and_is_logged_as_left_alone(
    provider: PostgresStorageProvider, caplog: pytest.LogCaptureFixture,
) -> None:
    store = provider.get_storage(WorkspaceSession)
    async with asyncio.timeout(_BOUND_S):
        await store.create(_row(ROW, SessionStatus.RUNNING))
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    with caplog.at_level(logging.INFO, logger="primer.workspace.session_reconcile"):
        reconciled = await _reconcile_blocked_on_a(
            provider,
            {"status": "ended", "ended_reason": "cancelled", "ended_at": ended_at.isoformat(), "turn_status": "idle"},
            list(NON_ENDED_STATUSES()),
        )

    async with asyncio.timeout(_BOUND_S):
        row = await store.get(ROW)
    assert row is not None
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled"), "the reconcile overwrote the reason another path ended the session with"
    assert row.ended_at == ended_at
    assert reconciled == 0, "a session the reconcile did not end is not counted"
    lines = [r.getMessage() for r in caplog.records if r.name == "primer.workspace.session_reconcile" and ROW in r.getMessage()]
    assert any("left alone" in m and "ended by another path" in m and "cancelled" in m for m in lines), lines
    assert not [r for r in caplog.records if r.name == "primer.workspace.session_reconcile" and r.levelno >= logging.WARNING]
