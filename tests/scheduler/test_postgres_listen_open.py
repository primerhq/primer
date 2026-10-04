"""PostgresScheduler._open_listen_connection when LISTEN cannot be set up.

If add_listener fails (or is cancelled) the freshly acquired connection must go
back to the pool, because nothing is registered to release it later. That
release can itself fail (asyncpg's holder.release re-raises errors from reset
or from waiting on a cancelled query), and a failing release must never replace
the exception being propagated: a CancelledError turned into an ordinary
Exception would be treated by the watcher as "open failed, retry", spending the
task's single cancel() and leaving drain_and_stop awaiting it forever.

No database needed: a fake pool and connection stand in for asyncpg.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.model.scheduler import PostgresSchedulerConfig
from primer.scheduler.postgres import PostgresScheduler


class _Conn:
    def __init__(self, add_listener_exc: BaseException) -> None:
        self._exc = add_listener_exc

    async def add_listener(self, channel, callback) -> None:
        raise self._exc


class _Pool:
    def __init__(self, conn: _Conn, release_exc: Exception | None = None) -> None:
        self._conn = conn
        self._release_exc = release_exc
        self.released: list[_Conn] = []

    async def acquire(self) -> _Conn:
        return self._conn

    async def release(self, conn: _Conn) -> None:
        self.released.append(conn)
        if self._release_exc is not None:
            raise self._release_exc


def _scheduler(pool: _Pool) -> PostgresScheduler:
    return PostgresScheduler(
        storage_provider=SimpleNamespace(pool=pool),  # type: ignore[arg-type]
        config=PostgresSchedulerConfig(),
    )


async def test_failed_listen_setup_releases_the_connection():
    pool = _Pool(_Conn(RuntimeError("add_listener failed")))
    sched = _scheduler(pool)

    with pytest.raises(RuntimeError, match="add_listener failed"):
        await sched._open_listen_connection("session_cancel")

    assert pool.released == [pool._conn]
    assert not sched._listeners  # never registered


async def test_failing_release_does_not_replace_a_cancellation():
    pool = _Pool(
        _Conn(asyncio.CancelledError()),
        release_exc=RuntimeError("reset failed during release"),
    )
    sched = _scheduler(pool)

    # The release error must be swallowed (logged), so the CancelledError the
    # caller is owed is the one that propagates.
    with pytest.raises(asyncio.CancelledError):
        await sched._open_listen_connection("session_cancel")

    assert pool.released == [pool._conn]  # the release was still attempted


async def test_failing_release_does_not_replace_an_ordinary_error():
    pool = _Pool(
        _Conn(ConnectionError("LISTEN refused")),
        release_exc=RuntimeError("reset failed during release"),
    )
    sched = _scheduler(pool)

    with pytest.raises(ConnectionError, match="LISTEN refused"):
        await sched._open_listen_connection("session_cancel")
