"""PostgresScheduler LISTEN watcher when its server connection DIES.

asyncpg never raises into the NOTIFY queue when the backend goes away (a
Postgres restart or failover, a network blip, ``pg_terminate_backend``). The
only signal is ``Connection.add_termination_listener``. Without it the watcher
parks on ``queue.get()`` forever and, for ``session_cancel``, every later user
cancel is silently never delivered (``WorkerPool._cancel_loop`` hard-preempts a
running turn only through this watcher).

No database needed: ``tests._listen_fakes`` models asyncpg's drop contract. The
live-Postgres twin of these tests is in ``test_postgres.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from primer.model.scheduler import PostgresSchedulerConfig
from primer.scheduler.postgres import PostgresScheduler
from tests._listen_fakes import FakeListenPool, eventually, stop

CHANNEL = "session_cancel"


def _scheduler() -> tuple[FakeListenPool, PostgresScheduler]:
    pool = FakeListenPool()
    sched = PostgresScheduler(
        storage_provider=SimpleNamespace(pool=pool),  # type: ignore[arg-type]
        config=PostgresSchedulerConfig(listen_reconnect_seconds=0.1),
    )
    return pool, sched


async def _watch(sched: PostgresScheduler, got: list[str]) -> None:
    async for sid in sched._watch_cancel("w1"):
        got.append(sid)


async def test_a_dropped_listen_connection_is_detected_and_resubscribed():
    pool, sched = _scheduler()
    got: list[str] = []
    task = asyncio.create_task(_watch(sched, got))
    try:
        await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")
        pool.conns[0].notify(CHANNEL, "before")
        await eventually(lambda: got == ["before"], what="the pre-drop cancel")

        pool.conns[0].drop()

        await eventually(
            lambda: len(pool.conns) == 2 and pool.conns[1].listeners,
            what="a second LISTEN connection after the drop",
        )
        pool.conns[1].notify(CHANNEL, "after")
        await eventually(lambda: got == ["before", "after"], what="the post-drop cancel")
        assert sched._listen_reconnects_total == 1
        assert pool.conns[0] in pool.released
    finally:
        await stop(task)


async def test_a_drop_is_one_warning_naming_the_channel_and_the_cause(caplog):
    """The operator-visible half of the fix: before it, a dead connection left
    no trace at all."""
    pool, sched = _scheduler()
    got: list[str] = []
    task = asyncio.create_task(_watch(sched, got))
    try:
        with caplog.at_level(logging.DEBUG, logger="primer.scheduler.postgres"):
            await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")
            pool.conns[0].drop()
            await eventually(lambda: len(pool.conns) == 2, what="the reconnect")
    finally:
        await stop(task)

    records = [
        r for r in caplog.records
        if r.name == "primer.scheduler.postgres" and r.levelno >= logging.WARNING
    ]
    assert [r.levelname for r in records] == ["WARNING"]
    message = records[0].getMessage()
    assert "session_cancel" in message and "terminated" in message and "reconnecting" in message
    # AGENTS.md bans the em-dash (U+2014); built from its code point so this
    # file does not itself contain the character.
    assert chr(0x2014) not in message
    assert records[0].exc_info is None


async def test_notifications_received_before_the_drop_are_delivered_first():
    pool, sched = _scheduler()
    got: list[str] = []
    task = asyncio.create_task(_watch(sched, got))
    try:
        await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")
        conn = pool.conns[0]
        # No await in between: both payloads and the drop are queued before the
        # watcher runs again, so the drop marker must not jump the line.
        conn.notify(CHANNEL, "a")
        conn.notify(CHANNEL, "b")
        conn.drop()

        await eventually(lambda: got == ["a", "b"], what="both queued cancels")
        await eventually(lambda: len(pool.conns) == 2, what="reconnect after the queue drained")
    finally:
        await stop(task)


async def test_the_watcher_keeps_retrying_while_the_server_is_still_down():
    pool, sched = _scheduler()
    got: list[str] = []
    task = asyncio.create_task(_watch(sched, got))
    try:
        await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")
        # A Postgres restart: the drop is followed by refused connection attempts.
        pool.fail_next_acquires(ConnectionRefusedError("down"), ConnectionRefusedError("down"))
        pool.conns[0].drop()

        await eventually(
            lambda: len(pool.conns) == 2 and pool.conns[1].listeners,
            what="a LISTEN connection once the server is back",
        )
        pool.conns[1].notify(CHANNEL, "after-restart")
        await eventually(lambda: got == ["after-restart"], what="the post-restart cancel")
        assert sched._listen_reconnects_total >= 1
    finally:
        await stop(task)


async def test_the_termination_callback_is_removed_when_the_watcher_closes():
    """asyncpg leaves a termination listener attached when a healthy connection
    goes back to the pool, so a later close of that pooled connection would
    push a drop marker into a queue nobody reads."""
    pool, sched = _scheduler()
    it = sched._watch_cancel("w1")

    async def consume_one() -> str:
        async for sid in it:
            return sid
        raise AssertionError("watcher ended without yielding")

    task = asyncio.create_task(consume_one())
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")
    conn = pool.conns[0]
    assert len(conn.termination_listeners) == 1, "the watcher must watch for connection loss"
    conn.notify(CHANNEL, "x")
    assert await asyncio.wait_for(task, 2.0) == "x"

    await it.aclose()

    assert conn.termination_listeners == set()
    assert conn in pool.released


async def test_cancelling_a_watcher_after_a_drop_raises_cancelled_error():
    """Shutdown after a drop: closing the dead connection fails on every call
    (asyncpg has already released the proxy). None of that may replace the
    CancelledError the caller is owed, or drain_and_stop's single cancel() is
    spent and the task keeps running."""
    pool, sched = _scheduler()
    got: list[str] = []
    task = asyncio.create_task(_watch(sched, got))
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="first LISTEN")

    pool.conns[0].drop()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    assert pool.conns[0] in pool.released
