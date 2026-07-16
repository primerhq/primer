"""Unit tests for the PostgresEventBus LISTEN reconnect loop.

Mirrors the scheduler's reconnect behaviour
(:meth:`primer.scheduler.postgres.PostgresScheduler._watch_channel`): a
dropped LISTEN connection is re-acquired and re-registered automatically,
without a real Postgres server. The fakes below stand in for asyncpg's
connection + pool so the supervisor's reconnect path can be driven
deterministically.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from primer.api.registries.provider_registry import ProviderRegistry
from primer.bus.postgres import PostgresEventBus, YIELD_EVENTS_CHANNEL
from primer.coordinator.postgres import PostgresInvalidationBus
from primer.int.coordinator import InvalidationTopic

pytestmark = pytest.mark.asyncio


class _FakeConn:
    """Minimal asyncpg-connection stand-in with a termination listener."""

    def __init__(self) -> None:
        self._notify_cb = None
        self._termination_cb = None
        self.listened = False
        self.released = False

    async def add_listener(self, channel, cb):
        assert channel == YIELD_EVENTS_CHANNEL
        self._notify_cb = cb
        self.listened = True

    async def remove_listener(self, channel, cb):
        self._notify_cb = None

    def add_termination_listener(self, cb):
        self._termination_cb = cb

    # --- test drivers ---
    def deliver(self, event_key: str, payload: dict) -> None:
        body = json.dumps({"event_key": event_key, "payload": payload})
        self._notify_cb(self, 0, YIELD_EVENTS_CHANNEL, body)

    def drop(self) -> None:
        """Simulate the server dropping the connection."""
        if self._termination_cb is not None:
            self._termination_cb(self)


class _FakePool:
    """Hands out a fresh _FakeConn per acquire; records every connection."""

    def __init__(self) -> None:
        self.conns: list[_FakeConn] = []

    async def acquire(self) -> _FakeConn:
        conn = _FakeConn()
        self.conns.append(conn)
        return conn

    async def release(self, conn: _FakeConn) -> None:
        conn.released = True


class _FakeStorage:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_subscribe_listens_and_delivers():
    pool = _FakePool()
    bus = PostgresEventBus(_FakeStorage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    sub = bus.subscribe()
    try:
        await _wait_for(lambda: pool.conns and pool.conns[0].listened)
        pool.conns[0].deliver("ask_user:1", {"x": 1})
        event = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
        assert event.event_key == "ask_user:1"
        assert event.payload == {"x": 1}
    finally:
        await sub.aclose()
        await bus.aclose()


async def test_reconnect_after_connection_drop():
    """When the LISTEN connection drops, the supervisor re-acquires a new
    connection, re-LISTENs, and resumes delivering events."""
    pool = _FakePool()
    bus = PostgresEventBus(_FakeStorage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    sub = bus.subscribe()
    try:
        await _wait_for(lambda: len(pool.conns) == 1 and pool.conns[0].listened)
        first = pool.conns[0]

        # Drop the connection -> supervisor must reconnect on a NEW conn.
        first.drop()
        await _wait_for(lambda: len(pool.conns) >= 2 and pool.conns[-1].listened)
        # The dropped connection was released back to the pool.
        await _wait_for(lambda: first.released)
        # The reconnect was counted in the bus metric.
        assert bus.metrics_snapshot()[
            "primer_yield_bus_listen_reconnects_total"
        ] >= 1

        # Events flow again over the reconnected listener.
        pool.conns[-1].deliver("ask_user:2", {"y": 2})
        event = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
        assert event.event_key == "ask_user:2"
    finally:
        await sub.aclose()
        await bus.aclose()


class _StubAdapter:
    """Sentinel cache entry with an awaitable aclose (never expected to
    run once the reconnect flush drops it)."""

    async def aclose(self) -> None:  # pragma: no cover - defensive
        return None


async def test_invalidation_reconnect_flushes_registry_caches():
    """A dropped-then-reconnected invalidation subscription must flush the
    ProviderRegistry caches wholesale.

    LISTEN/NOTIFY is not durable across a reconnect, so an invalidation
    NOTIFY (e.g. a rotated API key) emitted during the blip is lost and
    would otherwise leave a stale adapter cached until restart. The
    ``on_reconnect`` hook threaded bus -> invalidation-sub -> registry
    treats every cached adapter as potentially stale on reconnect.
    """
    pool = _FakePool()
    bus = PostgresEventBus(_FakeStorage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    inv_bus = PostgresInvalidationBus(bus)
    registry = ProviderRegistry(storage_provider=object())
    await registry.bind_invalidation_bus(inv_bus)
    try:
        # bind creates one EventBus subscription per topic (4), each on its
        # own LISTEN connection. Wait until all are listening.
        await _wait_for(
            lambda: sum(1 for c in pool.conns if c.listened) >= 4
        )

        # Seed every cache so we can observe the wholesale flush.
        registry._llm_cache["llm-1"] = _StubAdapter()
        registry._embedder_cache["emb-1"] = _StubAdapter()
        registry._cross_encoder_cache["ce-1"] = _StubAdapter()
        registry._toolset_cache["ts-1"] = _StubAdapter()

        # Drop one subscription's connection -> that supervisor reconnects
        # on a fresh conn and fires on_reconnect -> registry cache flush.
        first = pool.conns[0]
        first.drop()

        await _wait_for(
            lambda: (
                not registry._llm_cache
                and not registry._embedder_cache
                and not registry._cross_encoder_cache
                and not registry._toolset_cache
            )
        )
        # The bus also counted the reconnect (shared metric still bumps).
        assert bus.metrics_snapshot()[
            "primer_yield_bus_listen_reconnects_total"
        ] >= 1
    finally:
        await registry.aclose()
        await bus.aclose()


async def test_aclose_releases_connection_and_stops_supervisor():
    pool = _FakePool()
    bus = PostgresEventBus(_FakeStorage(pool), reconnect_seconds=0.01)
    await bus.initialize()
    sub = bus.subscribe()
    await _wait_for(lambda: pool.conns and pool.conns[0].listened)
    conn = pool.conns[0]
    await sub.aclose()
    await _wait_for(lambda: conn.released)
    # A closed subscription stops iterating.
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()
    await bus.aclose()


async def test_sub_aclose_deregisters_from_bus_registry():
    """A closed subscription must drop itself from the bus's ``_subs`` set so a
    long-lived bus does not leak a dead subscription per subscribe()/close
    cycle. Regression for the unbounded ``_subs`` leak under subscribe/close
    churn (e.g. one short-lived subscription per session turn).
    """
    pool = _FakePool()
    bus = PostgresEventBus(_FakeStorage(pool), reconnect_seconds=0.01)
    await bus.initialize()

    # Open and close many short-lived subscriptions; the registry must not
    # grow without bound.
    for _ in range(20):
        sub = bus.subscribe()
        assert sub in bus._subs
        await sub.aclose()
        assert sub not in bus._subs

    assert len(bus._subs) == 0

    # A double aclose() is a harmless no-op (idempotent discard).
    sub = bus.subscribe()
    await sub.aclose()
    await sub.aclose()
    assert sub not in bus._subs

    await bus.aclose()
