"""A Postgres bus subscription can be awaited until it is really listening (core review finding 12).

``subscribe()`` returns at once and the LISTEN happens later on a task, so an event published right after (the hold-a-fire pattern: subscribe,
then fire) could be missed. ``ready()`` resolves after the first successful LISTEN; callers that publish after subscribing await it.
"""
from __future__ import annotations

import asyncio

from primer.bus.in_memory import InMemoryEventBus
from primer.bus.postgres import PostgresEventBus


class _Conn:
    def __init__(self, gate: asyncio.Event, listening: list[str]) -> None:
        self._gate = gate
        self._listening = listening

    async def add_listener(self, channel, cb):
        await self._gate.wait()  # the LISTEN round trip
        self._listening.append(channel)

    async def remove_listener(self, channel, cb):
        return None

    def add_termination_listener(self, cb):
        return None

    def remove_termination_listener(self, cb):
        return None


class _Pool:
    def __init__(self, gate: asyncio.Event, listening: list[str]) -> None:
        self._gate, self._listening = gate, listening

    async def acquire(self):
        return _Conn(self._gate, self._listening)

    async def release(self, conn):
        return None


class _Storage:
    def __init__(self, pool) -> None:
        self.pool = pool


async def test_ready_waits_for_the_listen_to_finish():
    gate, listening = asyncio.Event(), []
    bus = PostgresEventBus(_Storage(_Pool(gate, listening)))
    sub = bus.subscribe()
    try:
        ready = asyncio.create_task(sub.ready())
        await asyncio.sleep(0.05)
        assert not ready.done() and listening == []  # subscribe() returned, but nothing listens yet
        gate.set()
        await asyncio.wait_for(ready, timeout=2)
        assert listening == ["primer_yield_events"]
    finally:
        await sub.aclose()
        await bus.aclose()


async def test_ready_does_not_hang_forever_when_the_database_is_down():
    gate, listening = asyncio.Event(), []  # the LISTEN never completes
    bus = PostgresEventBus(_Storage(_Pool(gate, listening)))
    sub = bus.subscribe()
    try:
        await asyncio.wait_for(sub.ready(timeout=0.1), timeout=2)  # gives up quietly; the supervisor keeps trying
        assert listening == []
    finally:
        await sub.aclose()
        await bus.aclose()


async def test_ready_returns_at_once_after_close_and_on_the_in_memory_bus():
    bus = InMemoryEventBus()
    await bus.initialize()
    sub = bus.subscribe()
    await asyncio.wait_for(sub.ready(), timeout=1)
    await sub.aclose()
    await asyncio.wait_for(sub.ready(), timeout=1)
    await bus.aclose()
