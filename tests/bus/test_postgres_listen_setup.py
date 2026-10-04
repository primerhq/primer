"""PostgresEventBus subscription supervisor: the pooled connection must go back
if LISTEN setup is cancelled or fails.

``_PostgresSubscription._run`` acquires a connection and then runs a LISTEN
round trip. The connection is only handed to the cleanup (``self._conn``) after
that round trip succeeds, so before this was fixed:

* a supervisor CANCELLED during the round trip leaked the connection. This is
  reachable on every session turn: ``dispatch._cancel_watcher`` subscribes at
  turn start and cancels at turn end. The pool then holds one connection for
  good and ``Pool.close()``, which waits for every acquired connection with no
  timeout, hangs ``storage_provider.aclose()`` until the pod is SIGKILLed;
* a LISTEN that FAILS on a connection that is still live (a timeout under the
  pool's command_timeout, hot-standby recovery) leaked one connection per retry,
  so the pool ran dry.

No database needed: ``tests._listen_fakes`` models asyncpg. The live-Postgres
twin is test_postgres_listen_setup_live.py.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from primer.bus.postgres import _PostgresSubscription
from tests._listen_fakes import FakeListenPool, eventually


def _subscription(pool: FakeListenPool) -> _PostgresSubscription:
    return _PostgresSubscription(
        acquire=pool.acquire, release=pool.release, reconnect_seconds=0.05,
    )


async def _cancel_supervisor(sub: _PostgresSubscription) -> None:
    sub._supervisor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sub._supervisor


@pytest.mark.parametrize("via", ["aclose", "supervisor.cancel"])
async def test_cancelled_during_the_listen_round_trip_releases_the_connection(via):
    gate = asyncio.Event()  # never set: the LISTEN round trip never finishes
    pool = FakeListenPool(configure=lambda c: setattr(c, "add_listener_gate", gate))
    sub = _subscription(pool)
    sub._start()
    await eventually(
        lambda: pool.conns and pool.conns[0].add_listener_entered.is_set(),
        what="the supervisor to be inside add_listener",
    )
    conn = pool.conns[0]

    if via == "aclose":
        await sub.aclose()
    else:
        await _cancel_supervisor(sub)

    assert pool.released == [conn], "the connection acquired for LISTEN was never released"


async def test_a_listen_that_fails_on_a_live_connection_releases_it_and_retries():
    pool = FakeListenPool(configure=lambda c: c.fail_methods.add("add_listener"))
    sub = _subscription(pool)
    sub._start()
    await eventually(lambda: len(pool.conns) >= 4, what="the supervisor to keep retrying")

    await sub.aclose()

    # Every connection it ever acquired went back, including the one that was
    # in flight when it was closed: nothing is stranded per retry.
    assert len(pool.conns) >= 4
    assert len(pool.released) == len(pool.conns)
    assert set(pool.released) == set(pool.conns)


async def test_cancelled_while_waiting_for_a_connection_has_nothing_to_release():
    """Control: the cancel lands in acquire(), before there is a connection."""
    pool = FakeListenPool()
    pool.acquire_gate = asyncio.Event()  # a pool with no free connection
    sub = _subscription(pool)
    sub._start()
    await asyncio.sleep(0.05)

    await sub.aclose()

    assert pool.conns == []
    assert pool.released == []


async def test_a_failing_release_does_not_replace_the_cancellation():
    """asyncpg's release can itself raise (a reset or a cancelled query that
    cannot be waited out). That must never replace the CancelledError the
    supervisor owes its caller."""
    gate = asyncio.Event()
    pool = FakeListenPool(configure=lambda c: setattr(c, "add_listener_gate", gate))
    pool.release_exc = RuntimeError("reset failed during release")
    sub = _subscription(pool)
    sub._start()
    await eventually(
        lambda: pool.conns and pool.conns[0].add_listener_entered.is_set(),
        what="the supervisor to be inside add_listener",
    )

    sub._supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sub._supervisor

    assert pool.released == [pool.conns[0]]  # the release was still attempted


@pytest.mark.parametrize("cancels", [2, 3])
async def test_a_second_cancel_during_the_unlisten_still_releases_the_connection(cancels):
    """The parked supervisor's exit path: the first cancel starts the cleanup,
    whose UNLISTEN is a round trip on the still-live connection. A SECOND cancel
    landing there (CancelledError is not an Exception, so the old cleanup did
    not catch it) skipped the release and stranded the connection: the pool kept
    it for good and Pool.close(), which waits for every acquired connection with
    no timeout, hung shutdown."""
    pool = FakeListenPool(configure=lambda c: setattr(c, "remove_listener_gate", asyncio.Event()))
    sub = _subscription(pool)
    sub._start()
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN to complete")
    conn = pool.conns[0]

    sub._supervisor.cancel()  # the first cancel: the cleanup begins and parks inside remove_listener
    await eventually(lambda: conn.remove_listener_entered.is_set(), what="the cleanup to reach UNLISTEN")
    for _ in range(cancels - 1):
        sub._supervisor.cancel()  # a further cancel while the UNLISTEN is in flight
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sub._supervisor, 2.0)
    assert pool.released == [conn], "a cancel inside UNLISTEN skipped the release"


async def test_a_listen_that_succeeds_is_still_released_exactly_once_on_close():
    """Control for the happy path: LISTEN completes, the supervisor parks, and
    closing it releases the connection once (no double release from the new
    cleanup)."""
    pool = FakeListenPool()
    sub = _subscription(pool)
    sub._start()
    await eventually(lambda: pool.conns and pool.conns[0].listeners, what="LISTEN to complete")

    await sub.aclose()

    assert pool.released == [pool.conns[0]]
