"""Tests for matrix.coordinator.in_memory.InMemoryInvalidationBus."""

from __future__ import annotations

import pytest

from matrix.coordinator.in_memory import InMemoryInvalidationBus
from matrix.int.coordinator import InvalidationTopic


@pytest.mark.asyncio
async def test_subscriber_receives_published_key():
    bus = InMemoryInvalidationBus()
    seen: list[str] = []

    async def handler(key: str) -> None:
        seen.append(key)

    await bus.subscribe(InvalidationTopic.LLM_PROVIDER, handler)
    await bus.publish(InvalidationTopic.LLM_PROVIDER, "terra")
    assert seen == ["terra"]


@pytest.mark.asyncio
async def test_publish_routes_only_to_matching_topic():
    bus = InMemoryInvalidationBus()
    llm_seen: list[str] = []
    embed_seen: list[str] = []

    async def llm_handler(key: str) -> None: llm_seen.append(key)
    async def embed_handler(key: str) -> None: embed_seen.append(key)

    await bus.subscribe(InvalidationTopic.LLM_PROVIDER, llm_handler)
    await bus.subscribe(InvalidationTopic.EMBEDDING_PROVIDER, embed_handler)

    await bus.publish(InvalidationTopic.LLM_PROVIDER, "terra")
    assert llm_seen == ["terra"]
    assert embed_seen == []


@pytest.mark.asyncio
async def test_subscriber_failure_is_swallowed():
    bus = InMemoryInvalidationBus()
    good_seen: list[str] = []

    async def good(key: str) -> None: good_seen.append(key)
    async def bad(key: str) -> None: raise RuntimeError("boom")

    await bus.subscribe(InvalidationTopic.LLM_PROVIDER, bad)
    await bus.subscribe(InvalidationTopic.LLM_PROVIDER, good)
    await bus.publish(InvalidationTopic.LLM_PROVIDER, "x")
    assert good_seen == ["x"]


@pytest.mark.asyncio
async def test_aclose_stops_delivery():
    bus = InMemoryInvalidationBus()
    seen: list[str] = []

    async def handler(key: str) -> None: seen.append(key)

    sub = await bus.subscribe(InvalidationTopic.LLM_PROVIDER, handler)
    await bus.publish(InvalidationTopic.LLM_PROVIDER, "a")
    await sub.aclose()
    await bus.publish(InvalidationTopic.LLM_PROVIDER, "b")
    assert seen == ["a"]
