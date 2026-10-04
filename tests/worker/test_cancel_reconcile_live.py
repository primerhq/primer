"""Cancel reconciliation against a REAL Postgres: a lost NOTIFY must not cost the cancel.

``cancel_session`` records ``cancel_requested`` on the session row and then sends a
``session_cancel`` NOTIFY. The NOTIFY is what hard-preempts a turn blocked in a long
LLM or tool call, and Postgres does not replay it: one sent while the pool's LISTEN
connection is down (the watcher sleeps ``listen_reconnect_seconds`` before it
resubscribes), or on a half-open connection nobody has noticed is dead, reaches
nobody. Before the reconciler the API answered 200 and the turn simply kept running.

These tests use the real storage provider, the real ``PostgresScheduler`` (whose LISTEN
backend is killed with ``pg_terminate_backend``) and a real started ``WorkerPool``.
The only stand-in is the turn itself: a task parked on a never-set event, anchored to a
real ``_CancelScope`` exactly as ``WorkerPool._run_turn`` registers one.

The reconnect sleep is far longer than every deadline below, so a turn preempted inside
the deadline cannot have been rescued by the resubscribed watcher: it was the row read.

Skipped unless PRIMER_TEST_POSTGRES_URL is set. The no-DB twin is test_cancel_reconcile.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.scheduler import PostgresSchedulerConfig, WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.postgres import PostgresScheduler
from primer.storage.postgres import PostgresStorageProvider
from primer.worker.pool import WorkerPool
from primer.worker.turn import _CancelScope

_URL_ENV = "PRIMER_TEST_POSTGRES_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_URL_ENV),
    reason=f"set {_URL_ENV} to run the live cancel-reconcile tests",
)

HEARTBEAT_S = 1
# The reconciler runs every HEARTBEAT_S; the margin is for a slow CI box, not for the
# feature. RECONNECT_S is well past DEADLINE_S, so the NOTIFY path cannot win the race.
DEADLINE_S = 3.5
RECONNECT_S = 12.0


def _config() -> PostgresConfig:
    p = urlparse(os.environ[_URL_ENV])
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
    schema = parse_qs(p.query).get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=p.port or 5432,
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


async def _eventually(predicate, *, timeout: float = 10.0, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for: {what}")
        await asyncio.sleep(0.02)


class _Stack:
    """A real storage provider, scheduler and started worker pool."""

    def __init__(self, sp, sched, pool) -> None:
        self.sp = sp
        self.sched = sched
        self.pool = pool
        self.sessions = sp.get_storage(WorkspaceSession)
        self.turns: list[asyncio.Task] = []
        self.session_ids: list[str] = []

    async def start_turn(self) -> tuple[str, asyncio.Task]:
        """A RUNNING session row plus a parked turn registered the way the pool does."""
        sid = f"s-reconcile-{uuid.uuid4().hex[:10]}"
        await self.sessions.create(
            WorkspaceSession(
                id=sid,
                workspace_id="ws-reconcile",
                binding=AgentSessionBinding(agent_id="ag-reconcile"),
                status=SessionStatus.RUNNING,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.session_ids.append(sid)
        scope = _CancelScope()
        key = (ClaimKind.SESSION, sid)

        async def turn() -> None:
            async with scope:
                self.pool._active_scopes[key] = scope
                try:
                    await asyncio.Event().wait()  # an LLM call that never returns
                finally:
                    self.pool._active_scopes.pop(key, None)

        task = asyncio.create_task(turn())
        self.turns.append(task)
        await _eventually(lambda: key in self.pool._active_scopes, what="the turn to register")
        return sid, task

    async def request_cancel(self, sid: str, *, notify: bool) -> None:
        """What ``cancel_session`` does to a RUNNING session, minus the event bus."""
        row = await self.sessions.get(sid)
        row.cancel_requested = True
        row.cancel_requested_at = datetime.now(timezone.utc)
        await self.sessions.update(row)
        if notify:
            await self.sched.signal_cancel(sid)

    async def kill_listen_backend(self) -> None:
        """What a Postgres failover or a network reset does to the scheduler's LISTEN."""
        await _eventually(lambda: len(self.sched._listeners) >= 1, what="the cancel LISTEN")
        pids = {ln.conn.get_server_pid() for ln in self.sched._listeners}
        async with self.sp.pool.acquire() as conn:
            for pid in pids:
                assert await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
        await _eventually(
            lambda: not any(ln.conn.get_server_pid() in pids for ln in self.sched._listeners),
            what="the watcher to notice the dead LISTEN connection",
        )


async def _make_stack(*, reconnect_seconds: float) -> _Stack:
    sp = PostgresStorageProvider(_config())
    await sp.initialize()
    async with sp.pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS workers")
    sched = PostgresScheduler(
        storage_provider=sp,
        config=PostgresSchedulerConfig(listen_reconnect_seconds=reconnect_seconds),
    )
    await sched.initialize()
    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=1, heartbeat_interval_seconds=HEARTBEAT_S, lease_ttl_seconds=5,
        ),
        scheduler=sched,
        storage=sp,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )
    await pool.start()
    return _Stack(sp, sched, pool)


async def _teardown(stack: _Stack) -> None:
    for task in stack.turns:
        task.cancel()
    for task in stack.turns:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stack.pool.drain_and_stop(timeout=2), 10.0)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stack.sched.aclose(), 5.0)
    for sid in stack.session_ids:
        with contextlib.suppress(Exception):
            await stack.sessions.delete(sid)
    try:
        await asyncio.wait_for(stack.sp.aclose(), 5.0)
    except TimeoutError:
        stack.sp.pool.terminate()
        pytest.fail("pool.close() hung: a connection was never released")


@pytest.fixture
async def stack():
    s = await _make_stack(reconnect_seconds=RECONNECT_S)
    try:
        yield s
    finally:
        await _teardown(s)


async def _preempted_within(task: asyncio.Task, seconds: float) -> float | None:
    """Seconds until ``task`` was cancelled, or None if it is still running after ``seconds``."""
    started = asyncio.get_running_loop().time()
    await asyncio.wait({task}, timeout=seconds)
    if not task.done():
        return None
    assert task.cancelled(), "the turn ended, but not by being preempted"
    return asyncio.get_running_loop().time() - started


async def test_a_cancel_sent_while_the_listen_connection_is_reconnecting_still_preempts(stack):
    """The reported bug. The LISTEN backend dies, the watcher is asleep for RECONNECT_S
    before it resubscribes, and the user's Cancel (row write + NOTIFY) lands in that
    gap: the NOTIFY has no listener, and before the reconciler nothing else preempted
    the turn."""
    sid, turn = await stack.start_turn()
    await stack.kill_listen_backend()

    await stack.request_cancel(sid, notify=True)

    took = await _preempted_within(turn, DEADLINE_S)
    assert took is not None, (
        f"the turn was still running {DEADLINE_S}s after a cancel whose NOTIFY had no "
        f"listener (the watcher resubscribes only after {RECONNECT_S}s)"
    )
    assert stack.pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 1


async def test_a_cancel_whose_notify_never_arrives_is_still_preempted_from_the_row(stack):
    """No dead connection to blame: only the row says cancel. This is the half-open
    connection, the startup race and every other way a NOTIFY can go missing."""
    sid, turn = await stack.start_turn()

    await stack.request_cancel(sid, notify=False)

    took = await _preempted_within(turn, DEADLINE_S)
    assert took is not None, "a cancel recorded on the row never reached the running turn"
    assert stack.pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 1


async def test_a_cancel_over_a_healthy_listen_connection_is_preempted_at_once_and_not_counted(
    stack,
):
    """Control: the NOTIFY path is still the fast path, and the reconciler, finding the
    turn already gone, neither double-cancels it nor counts it."""
    sid, turn = await stack.start_turn()
    await _eventually(lambda: len(stack.sched._listeners) >= 1, what="the cancel LISTEN")

    await stack.request_cancel(sid, notify=True)

    took = await _preempted_within(turn, HEARTBEAT_S * 0.8)
    assert took is not None, "the NOTIFY path no longer preempts within a heartbeat"
    await asyncio.sleep(HEARTBEAT_S * 1.5)  # let the reconciler run at least once more
    assert stack.pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 0


async def test_a_session_nobody_cancelled_is_left_running(stack):
    sid, turn = await stack.start_turn()

    assert await _preempted_within(turn, HEARTBEAT_S * 2.5) is None
    assert stack.pool.metrics_snapshot()["primer_worker_cancels_reconciled_total"] == 0
