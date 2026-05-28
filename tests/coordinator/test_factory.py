"""Tests for primer.coordinator.factory.CoordinatorFactory."""

from __future__ import annotations

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.coordinator.factory import CoordinatorFactory
from primer.coordinator.in_memory import (
    InMemoryInvalidationBus,
    InMemoryLeaderElector,
    InMemoryRateLimiter,
)
from primer.int.coordinator import Coordinator


@pytest.mark.asyncio
async def test_factory_returns_in_memory_for_in_memory_bus(fake_storage_provider):
    bus = InMemoryEventBus()
    await bus.initialize()
    coord = CoordinatorFactory.create(
        storage_provider=fake_storage_provider,
        event_bus=bus,
        owner_id="api-test",
    )
    assert isinstance(coord, Coordinator)
    assert isinstance(coord.rate_limiter, InMemoryRateLimiter)
    assert isinstance(coord.invalidation_bus, InMemoryInvalidationBus)
    assert isinstance(coord.leader_elector, InMemoryLeaderElector)
    await bus.aclose()
