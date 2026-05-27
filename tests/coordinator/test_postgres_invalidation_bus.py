"""Tests for PostgresInvalidationBus.

Uses the in-memory event bus (which is the same EventBus protocol) so
this test exercises the wrapper without needing a real Postgres bus.
End-to-end Postgres-bus verification will happen in the Task 15
distributed smoke; the wrapper is bus-agnostic by design.
"""

from __future__ import annotations

import asyncio

import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.coordinator.postgres import PostgresInvalidationBus
from matrix.int.coordinator import InvalidationTopic


@pytest.mark.asyncio
async def test_publish_and_subscribe_round_trip():
    bus = InMemoryEventBus()
    await bus.initialize()
    inv = PostgresInvalidationBus(bus)
    seen: list[str] = []

    async def handler(key: str) -> None:
        seen.append(key)

    sub = await inv.subscribe(InvalidationTopic.LLM_PROVIDER, handler)
    await inv.publish(InvalidationTopic.LLM_PROVIDER, "terra")

    # The wrapper's subscription consumes via async-iterator; let the
    # loop deliver the event.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if seen:
            break
    assert seen == ["terra"]
    await sub.aclose()
    await bus.aclose()


@pytest.mark.asyncio
async def test_subscriber_filters_by_topic_prefix():
    """Publishing on EMBEDDING_PROVIDER does NOT reach an LLM_PROVIDER
    subscriber even though both share the same bus."""
    bus = InMemoryEventBus()
    await bus.initialize()
    inv = PostgresInvalidationBus(bus)
    seen: list[str] = []

    async def handler(key: str) -> None: seen.append(key)

    sub = await inv.subscribe(InvalidationTopic.LLM_PROVIDER, handler)
    await inv.publish(InvalidationTopic.EMBEDDING_PROVIDER, "x")
    await asyncio.sleep(0.05)
    assert seen == []

    await inv.publish(InvalidationTopic.LLM_PROVIDER, "y")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if seen:
            break
    assert seen == ["y"]

    await sub.aclose()
    await bus.aclose()


@pytest.mark.asyncio
async def test_aclose_stops_delivery():
    bus = InMemoryEventBus()
    await bus.initialize()
    inv = PostgresInvalidationBus(bus)
    seen: list[str] = []

    async def handler(key: str) -> None: seen.append(key)

    sub = await inv.subscribe(InvalidationTopic.LLM_PROVIDER, handler)
    await inv.publish(InvalidationTopic.LLM_PROVIDER, "a")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if seen:
            break
    await sub.aclose()

    await inv.publish(InvalidationTopic.LLM_PROVIDER, "b")
    await asyncio.sleep(0.05)
    assert seen == ["a"]
    await bus.aclose()
