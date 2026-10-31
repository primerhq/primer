"""Bus fan-out: who receives what, how many connections it takes, and how much a slow subscriber may hold (perf plan Task 6, finding H3).

Characterization (both buses, pass on the unchanged code): a published event reaches every subscriber that should see it exactly once and nobody
else, in publish order; a subscription opened after a publish sees nothing of it; a closed subscription frees its registry entry.
New shape (red first): subscribers may ask for one exact key or a key prefix; one Postgres process holds ONE listener connection however many
subscriptions it has and parses each notification once; every queue is bounded (oldest dropped, counted), and a ``coalesce`` subscription (cancel,
terminal) keeps only its newest event so a stop request is never lost to a backlog.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import primer.bus.postgres as pg_module
from primer.bus.in_memory import InMemoryEventBus
from primer.bus.postgres import YIELD_EVENTS_CHANNEL, PostgresEventBus

pytestmark = pytest.mark.asyncio


class _Conn:
    def __init__(self) -> None:
        self.notify_cb = None
        self.termination_cb = None
        self.released = False

    async def add_listener(self, channel, cb):
        assert channel == YIELD_EVENTS_CHANNEL
        self.notify_cb = cb

    async def remove_listener(self, channel, cb):
        self.notify_cb = None

    def add_termination_listener(self, cb):
        self.termination_cb = cb

    def remove_termination_listener(self, cb):
        self.termination_cb = None


class _Pool:
    def __init__(self) -> None:
        self.conns: list[_Conn] = []

    async def acquire(self):
        conn = _Conn()
        self.conns.append(conn)
        return conn

    async def release(self, conn):
        conn.released = True


class _Storage:
    def __init__(self, pool) -> None:
        self.pool = pool


class _Harness:
    """One bus, either flavour, with a ``publish`` that reaches its subscribers the way production does."""

    def __init__(self, kind: str, **kw) -> None:
        self.kind = kind
        self.pool = _Pool()
        self.bus = InMemoryEventBus(**kw) if kind == "memory" else PostgresEventBus(_Storage(self.pool), reconnect_seconds=0.01, **kw)

    async def start(self):
        await self.bus.initialize()
        return self

    async def publish(self, key, payload=None):
        if self.kind == "memory":
            await self.bus.publish(key, payload)
            return
        # NOTIFY delivery: the listener connection's callback receives the JSON body.
        await self.listening()
        body = json.dumps({"event_key": key, "payload": payload or {}})
        for conn in self.pool.conns:  # a NOTIFY reaches every connection that is listening
            if conn.notify_cb is not None:
                conn.notify_cb(None, 0, YIELD_EVENTS_CHANNEL, body)

    @property
    def registry(self):
        return self.bus._subs if self.kind == "postgres" else self.bus._subscribers

    async def listening(self):
        for _ in range(200):
            if self.pool.conns and all(c.notify_cb is not None for c in self.pool.conns if not c.released):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the bus never started listening")

    async def aclose(self):
        await self.bus.aclose()


@pytest.fixture(params=["memory", "postgres"])
async def h(request):
    harness = await _Harness(request.param).start()
    yield harness
    await harness.aclose()


async def _take(sub, n, timeout=2.0):
    return [await asyncio.wait_for(sub.__anext__(), timeout) for _ in range(n)]


async def _no_more(sub, timeout=0.1):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.__anext__(), timeout)


# -- characterization --------------------------------------------------------------------------------------------------------------------------


async def test_every_firehose_subscriber_gets_every_event_once_in_publish_order(h):
    a, b = h.bus.subscribe(), h.bus.subscribe()
    await a.ready()
    await b.ready()
    for i in range(5):
        await h.publish(f"k:{i % 2}", {"i": i})
    for sub in (a, b):
        got = await _take(sub, 5)
        assert [e.payload["i"] for e in got] == [0, 1, 2, 3, 4]
        await _no_more(sub)
    await a.aclose()
    await b.aclose()


async def test_a_subscription_opened_after_a_publish_sees_nothing_of_it(h):
    first = h.bus.subscribe()
    await first.ready()
    await h.publish("k:early")
    late = h.bus.subscribe()
    await late.ready()
    await h.publish("k:late")
    assert [e.event_key for e in await _take(late, 1)] == ["k:late"]
    await _no_more(late)
    await first.aclose()
    await late.aclose()


async def test_closing_a_subscription_frees_its_registry_entry_and_stops_delivery(h):
    sub = h.bus.subscribe()
    await sub.ready()
    assert sub in h.registry
    await sub.aclose()
    assert sub not in h.registry and len(h.registry) == 0
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


# -- new shape ---------------------------------------------------------------------------------------------------------------------------------


async def test_an_exact_key_subscriber_receives_only_that_key(h):
    cancel = h.bus.subscribe(key="session:s1:cancel", coalesce=True)
    everything = h.bus.subscribe()
    await cancel.ready()
    await everything.ready()
    await h.publish("session:s1:tick")
    await h.publish("session:s2:cancel")
    await h.publish("session:s1:cancel", {"why": "stop"})
    assert [e.event_key for e in await _take(cancel, 1)] == ["session:s1:cancel"]
    await _no_more(cancel)
    assert len(await _take(everything, 3)) == 3
    await cancel.aclose()
    await everything.aclose()


async def test_a_prefix_subscriber_receives_only_keys_under_the_prefix(h):
    inv = h.bus.subscribe(prefix="invalidate:llm:")
    await inv.ready()
    for key in ("invalidate:toolset:x", "invalidate:llm:p1", "other", "invalidate:llm:p2"):
        await h.publish(key)
    assert [e.event_key for e in await _take(inv, 2)] == ["invalidate:llm:p1", "invalidate:llm:p2"]
    await _no_more(inv)
    await inv.aclose()


async def test_a_prefix_and_suffix_subscriber_receives_only_matching_keys(h):
    terminal = h.bus.subscribe(prefix="session:", suffix=":terminal")
    await terminal.ready()
    for key in ("session:s1:tick", "session:s1:terminal", "other:s2:terminal", "session:s2:terminal"):
        await h.publish(key)
    assert [e.event_key for e in await _take(terminal, 2)] == ["session:s1:terminal", "session:s2:terminal"]
    await _no_more(terminal)
    await terminal.aclose()


async def test_a_coalescing_subscriber_keeps_only_the_newest_event():
    h = await _Harness("memory").start()
    sub = h.bus.subscribe(key="session:s1:cancel", coalesce=True)
    for i in range(3):
        await h.publish("session:s1:cancel", {"n": i})
    assert [e.payload["n"] for e in await _take(sub, 1)] == [2]
    await _no_more(sub)
    await h.aclose()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_a_stalled_subscriber_never_holds_more_than_its_bound(kind):
    h = await _Harness(kind, queue_maxsize=5).start()
    stalled, healthy = h.bus.subscribe(), h.bus.subscribe()
    await stalled.ready()
    await healthy.ready()
    seen = []

    async def drain():
        async for event in healthy:
            seen.append(event.payload["i"])

    task = asyncio.create_task(drain())
    for i in range(50):
        await h.publish("k", {"i": i})
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert stalled._mailbox.qsize() <= 5 and stalled._mailbox.dropped == 45
    assert [e.payload["i"] for e in await _take(stalled, 5)] == [45, 46, 47, 48, 49], "the newest events are the ones kept"
    assert seen == list(range(50)), "a healthy subscriber is unaffected by a stalled one"
    await healthy.aclose()
    await task
    await stalled.aclose()
    await h.aclose()


async def test_a_hundred_subscriptions_hold_one_listener_connection_and_parse_each_notification_once(monkeypatch):
    h = await _Harness("postgres").start()
    parses = {"n": 0}
    real_json = pg_module.json

    class _CountingJson:
        def __getattr__(self, name):
            return getattr(real_json, name)

        @staticmethod
        def loads(s, *a, **k):
            parses["n"] += 1
            return real_json.loads(s, *a, **k)

    monkeypatch.setattr(pg_module, "json", _CountingJson())
    subs = [h.bus.subscribe() for _ in range(100)]
    await subs[0].ready()
    assert len(h.pool.conns) == 1, "one LISTEN connection per process, not one per subscription"
    await h.publish("unrelated:key")
    assert parses["n"] == 1, "a notification is decoded once, not once per subscriber"
    got = await asyncio.gather(*(_take(s, 1) for s in subs))
    assert all(len(g) == 1 and g[0].event_key == "unrelated:key" for g in got)
    for s in subs:
        await s.aclose()
    await h.aclose()


async def test_the_listener_connection_is_released_when_the_last_subscription_closes():
    h = await _Harness("postgres").start()
    a, b = h.bus.subscribe(), h.bus.subscribe()
    await a.ready()
    conn = h.pool.conns[0]
    await a.aclose()
    assert not conn.released, "another subscription is still listening"
    await b.aclose()
    for _ in range(100):
        if conn.released:
            break
        await asyncio.sleep(0.01)
    assert conn.released
    again = h.bus.subscribe()  # a later subscription starts a fresh listener
    await again.ready()
    assert len(h.pool.conns) == 2
    await again.aclose()
    await h.aclose()


async def test_an_exact_key_subscription_survives_a_reconnect_and_keeps_receiving():
    h = await _Harness("postgres").start()
    sub = h.bus.subscribe(key="session:s1:cancel", coalesce=True)
    await sub.ready()
    h.pool.conns[0].termination_cb(h.pool.conns[0])  # the server drops the connection
    for _ in range(200):
        if len(h.pool.conns) >= 2 and h.pool.conns[-1].notify_cb is not None:
            break
        await asyncio.sleep(0.01)
    await h.publish("session:s1:cancel")
    assert [e.event_key for e in await _take(sub, 1)] == ["session:s1:cancel"]
    await sub.aclose()
    await h.aclose()
