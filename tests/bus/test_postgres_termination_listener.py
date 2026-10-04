"""PostgresEventBus subscriptions must take their termination callback off the
pooled connection when they release it.

asyncpg's ``reset()`` does not clear a connection's termination listeners, so a
callback left attached survives the release back to the pool. A bus makes one
subscription per session turn (``dispatch._cancel_watcher``), and a hot,
LIFO-ordered pooled connection is handed out again and again, so the callbacks
(each holding an ``asyncio.Event``) pile up on it until the connection finally
closes, when asyncpg calls every one of them at once.

No database needed: ``tests._listen_fakes`` models asyncpg. ``reuse_connection``
makes the fake pool hand out the same connection every time.
"""

from __future__ import annotations

import asyncio

from primer.bus.postgres import _PostgresSubscription
from tests._listen_fakes import FakeListenPool, eventually, stop


def _subscription(pool: FakeListenPool) -> _PostgresSubscription:
    return _PostgresSubscription(
        acquire=pool.acquire, release=pool.release, reconnect_seconds=0.1,
    )


async def _open_and_close(pool: FakeListenPool, cycle: int) -> None:
    sub = _subscription(pool)
    sub._start()
    # Wait until THIS subscription is parked on the live connection. Count the
    # attach calls rather than inspecting the callback set: a leak keeps that
    # set non-empty, which would let later cycles be cancelled before they
    # even started and hide what the test is about.
    await eventually(
        lambda: pool.conns and pool.conns[0].calls.count("add_termination_listener") == cycle + 1,
        what=f"subscription {cycle} to be listening",
    )
    await sub.aclose()


async def test_closing_many_subscriptions_leaves_no_termination_callbacks():
    pool = FakeListenPool(reuse_connection=True)
    cycles = 25

    for cycle in range(cycles):
        await _open_and_close(pool, cycle)

    (conn,) = pool.conns
    assert len(pool.released) == cycles
    assert conn.termination_listeners == set(), (
        f"{len(conn.termination_listeners)} termination callbacks leaked onto "
        "the pooled connection"
    )


async def test_a_failing_callback_removal_does_not_stop_the_release():
    pool = FakeListenPool()
    sub = _subscription(pool)
    sub._start()
    await eventually(lambda: pool.conns and pool.conns[0].termination_listeners, what="listening")
    conn = pool.conns[0]
    conn.fail_methods.add("remove_termination_listener")

    await sub.aclose()

    assert "remove_termination_listener" in conn.calls  # it was attempted
    assert "remove_listener" in conn.calls              # and the next step still ran
    assert pool.released == [conn]                      # and the connection went back


async def test_a_dropped_connection_is_replaced_and_leaves_nothing_attached():
    pool = FakeListenPool()
    sub = _subscription(pool)
    sub._start()
    try:
        await eventually(lambda: pool.conns and pool.conns[0].termination_listeners, what="listening")
        first = pool.conns[0]

        first.drop()  # asyncpg clears its callbacks, and every call on it now raises

        await eventually(
            lambda: len(pool.conns) == 2 and pool.conns[1].termination_listeners,
            what="a replacement connection, listening",
        )
        assert first in pool.released
        assert first.termination_listeners == set()
        second = pool.conns[1]
    finally:
        await stop(sub._supervisor)
        await sub.aclose()

    assert second.termination_listeners == set()
    assert second in pool.released


async def test_cancelling_a_subscription_after_a_drop_still_cancels():
    """The release of a dead connection fails on every call; that must not
    replace the CancelledError the supervisor owes its caller."""
    pool = FakeListenPool()
    sub = _subscription(pool)
    sub._start()
    await eventually(lambda: pool.conns and pool.conns[0].termination_listeners, what="listening")

    pool.conns[0].drop()
    sub._supervisor.cancel()

    try:
        await asyncio.wait_for(sub._supervisor, 2.0)
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover
        raise AssertionError("the supervisor swallowed its cancellation")
    assert pool.conns[0] in pool.released
