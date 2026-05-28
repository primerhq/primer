"""Tests that ProviderRegistry subscribes to InvalidationBus and that
publishing on the bus reaches the registry's local cache eviction."""

from __future__ import annotations

import asyncio

import pytest

from primer.coordinator.in_memory import InMemoryInvalidationBus
from primer.int.coordinator import InvalidationTopic


@pytest.mark.asyncio
async def test_registry_invalidates_llm_when_bus_publishes(
    fake_storage_provider,
):
    """Publishing on the bus reaches the registry's invalidate_llm
    code path via the registered handler."""
    from primer.api.registries.provider_registry import ProviderRegistry

    bus = InMemoryInvalidationBus()
    registry = ProviderRegistry(storage_provider=fake_storage_provider)
    await registry.bind_invalidation_bus(bus)

    # Seed the cache directly so we can observe eviction
    registry._llm_cache["fake-id"] = ("sentinel",)  # type: ignore[attr-defined]
    assert "fake-id" in registry._llm_cache

    await bus.publish(InvalidationTopic.LLM_PROVIDER, "fake-id")
    # Give the subscription handler a chance to run
    await asyncio.sleep(0)
    assert "fake-id" not in registry._llm_cache

    await registry.aclose()


@pytest.mark.asyncio
async def test_invalidate_llm_publishes_to_bus_when_bound(
    fake_storage_provider,
):
    """Calling the public invalidate_llm() routes through the bus when
    one is bound; the cache eviction still happens (via the subscription
    handler firing inside the publish call)."""
    from primer.api.registries.provider_registry import ProviderRegistry

    bus = InMemoryInvalidationBus()
    registry = ProviderRegistry(storage_provider=fake_storage_provider)
    await registry.bind_invalidation_bus(bus)

    registry._llm_cache["x"] = ("v",)  # type: ignore[attr-defined]
    await registry.invalidate_llm("x")
    assert "x" not in registry._llm_cache

    await registry.aclose()


@pytest.mark.asyncio
async def test_invalidate_llm_works_without_bus(fake_storage_provider):
    """Legacy path: a registry constructed without binding the bus still
    invalidates the local cache when invalidate_llm() is called."""
    from primer.api.registries.provider_registry import ProviderRegistry

    registry = ProviderRegistry(storage_provider=fake_storage_provider)
    registry._llm_cache["x"] = ("v",)  # type: ignore[attr-defined]
    await registry.invalidate_llm("x")
    assert "x" not in registry._llm_cache
