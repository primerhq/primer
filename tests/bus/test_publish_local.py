"""``publish_local``: deliver to this process's subscribers without a NOTIFY (perf plan Task 7, finding M7).

A token delta rides the bus only so the tap/SSE reader can show it live. When the publisher and every reader are the same process, a NOTIFY is
pure cost (Postgres serialises NOTIFYs cluster-wide and sends each payload to every listening backend). ``publish_local`` hands the event to
this process's subscriptions directly; ``publish`` is unchanged and still crosses processes.
"""
from __future__ import annotations

import asyncio

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.bus.postgres import YIELD_EVENTS_CHANNEL, PostgresEventBus
from primer.int.event_bus import EventBus

pytestmark = pytest.mark.asyncio


class _Conn:
    def __init__(self) -> None:
        self.notify_cb = None
        self.executed: list[str] = []

    async def add_listener(self, channel, cb):
        self.notify_cb = cb

    async def remove_listener(self, channel, cb):
        self.notify_cb = None

    def add_termination_listener(self, cb):
        return None

    def remove_termination_listener(self, cb):
        return None

    async def execute(self, sql, *args):
        self.executed.append(sql)


class _Pool:
    def __init__(self) -> None:
        self.conns: list[_Conn] = []
        self.notifies = 0

    async def acquire(self):
        conn = _Conn()
        self.conns.append(conn)
        return conn

    async def release(self, conn):
        return None

    def acquire_ctx(self):  # `async with pool.acquire() as conn` is how publish() takes a connection
        raise NotImplementedError


class _AcquireCtx:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, *args):
        if "pg_notify" in sql:
            self._pool.notifies += 1


class _CountingPool(_Pool):
    """acquire() both awaitable (the LISTEN supervisor) and an async context manager (publish)."""

    def acquire(self):  # type: ignore[override]
        pool = self

        class _Both:
            def __await__(inner):
                async def _go():
                    conn = _Conn()
                    pool.conns.append(conn)
                    return conn

                return _go().__await__()

            async def __aenter__(inner):
                return _AcquireCtx(pool)

            async def __aexit__(inner, *a):
                return False

        return _Both()


class _Storage:
    def __init__(self, pool) -> None:
        self.pool = pool


async def test_postgres_publish_local_reaches_local_subscribers_once_and_sends_no_notify():
    pool = _CountingPool()
    bus = PostgresEventBus(_Storage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    exact = bus.subscribe(key="session:s1:delta")
    firehose = bus.subscribe()
    other = bus.subscribe(key="session:s2:delta")
    await exact.ready()
    await bus.publish_local("session:s1:delta", {"text": "hi"})
    assert pool.notifies == 0, "a local publish must not NOTIFY"
    assert (await asyncio.wait_for(exact.__anext__(), 1)).payload == {"text": "hi"}
    assert (await asyncio.wait_for(firehose.__anext__(), 1)).event_key == "session:s1:delta"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(exact.__anext__(), 0.1)  # exactly once
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other.__anext__(), 0.1)
    for sub in (exact, firehose, other):
        await sub.aclose()
    await bus.aclose()


async def test_postgres_publish_still_notifies():
    pool = _CountingPool()
    bus = PostgresEventBus(_Storage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    await bus.publish("session:s1:tick", {"seq": 1})
    assert pool.notifies == 1
    await bus.aclose()


async def test_publish_local_on_a_closed_postgres_bus_raises_like_publish():
    bus = PostgresEventBus(_Storage(_CountingPool()))
    await bus.aclose()
    with pytest.raises(RuntimeError):
        await bus.publish_local("k", {})


async def test_the_in_memory_bus_publish_local_is_publish():
    bus = InMemoryEventBus()
    sub = bus.subscribe(key="session:s1:delta")
    await bus.publish_local("session:s1:delta", {"n": 1})
    assert (await asyncio.wait_for(sub.__anext__(), 1)).payload == {"n": 1}
    await sub.aclose()
    await bus.aclose()


async def test_a_bus_that_defines_only_publish_gets_the_base_class_fallback():
    seen: list[tuple[str, dict]] = []

    class _Minimal(EventBus):
        async def initialize(self): ...
        async def aclose(self): ...

        async def publish(self, event_key, payload=None):
            seen.append((event_key, payload or {}))

        def subscribe(self, **kw): ...

    await _Minimal().publish_local("k", {"a": 1})
    assert seen == [("k", {"a": 1})]
