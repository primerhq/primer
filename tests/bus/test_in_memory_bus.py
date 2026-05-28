"""Unit tests for the in-process event bus implementation.

Verifies publish/subscribe basics, broadcast semantics, and close
behaviour. The postgres bus has its own integration test that runs
under the scheduler postgres fixture.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.bus.in_memory import InMemoryEventBus


@pytest.fixture
async def bus():
    b = InMemoryEventBus()
    await b.initialize()
    yield b
    await b.aclose()


@pytest.mark.asyncio
class TestInMemoryEventBus:
    async def test_publish_then_subscribe_observes_event(self, bus):
        sub = bus.subscribe()
        try:
            await bus.publish("timer:tc-1", {"x": 1})
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.event_key == "timer:tc-1"
            assert event.payload == {"x": 1}
        finally:
            await sub.aclose()

    async def test_subscribe_before_publish(self, bus):
        sub = bus.subscribe()
        try:
            # Publish AFTER subscription — event must land.
            await bus.publish("watch:tc-2", {"foo": "bar"})
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.event_key == "watch:tc-2"
        finally:
            await sub.aclose()

    async def test_publish_before_subscribe_event_is_lost(self, bus):
        # Bus is fire-and-forget — subscribers don't see events
        # published before they subscribed. Documented behaviour.
        await bus.publish("timer:tc-x", {})
        sub = bus.subscribe()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(sub), timeout=0.1)
        finally:
            await sub.aclose()

    async def test_broadcast_two_subscribers_both_see_event(self, bus):
        a = bus.subscribe()
        b = bus.subscribe()
        try:
            await bus.publish("timer:tc-3", {"n": 42})
            ea = await asyncio.wait_for(anext(a), timeout=1.0)
            eb = await asyncio.wait_for(anext(b), timeout=1.0)
            assert ea.event_key == "timer:tc-3"
            assert eb.event_key == "timer:tc-3"
            assert ea.payload == {"n": 42}
            assert eb.payload == {"n": 42}
        finally:
            await a.aclose()
            await b.aclose()

    async def test_payload_is_defensive_copy(self, bus):
        # Mutating the published payload after publish must not
        # leak into observed events — defensive copy at the bus
        # boundary.
        sub = bus.subscribe()
        try:
            payload = {"key": "original"}
            await bus.publish("timer:tc-mut", payload)
            payload["key"] = "mutated"
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.payload == {"key": "original"}
        finally:
            await sub.aclose()

    async def test_close_subscription_stops_iteration(self, bus):
        sub = bus.subscribe()
        await sub.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(sub)

    async def test_close_bus_closes_all_subscriptions(self):
        b = InMemoryEventBus()
        await b.initialize()
        sub = b.subscribe()
        await b.aclose()
        # After bus close, the subscription is also closed.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(sub), timeout=1.0)

    async def test_publish_on_closed_bus_raises(self):
        b = InMemoryEventBus()
        await b.initialize()
        await b.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await b.publish("timer:tc-x", {})

    async def test_publish_none_payload_yields_empty_dict(self, bus):
        sub = bus.subscribe()
        try:
            await bus.publish("timer:tc-none")  # payload omitted
            event = await asyncio.wait_for(anext(sub), timeout=1.0)
            assert event.payload == {}
        finally:
            await sub.aclose()
