"""Factory that builds a :class:`Coordinator` matching the bus type.

In-memory bus → in-memory coordinator (single mode).
Postgres bus → falls through to in-memory for now; Postgres backends
land in Task 15 of the migration plan.

Selection mirrors the existing scheduler/bus factory pattern so a single
runtime-mode choice configures the whole stack consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from matrix.bus.in_memory import InMemoryEventBus
from matrix.coordinator.in_memory import (
    InMemoryInvalidationBus,
    InMemoryLeaderElector,
    InMemoryRateLimiter,
)
from matrix.int.coordinator import Coordinator
from matrix.int.event_bus import EventBus

if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


class CoordinatorFactory:
    @staticmethod
    def create(
        *,
        storage_provider: "StorageProvider",
        event_bus: EventBus,
        owner_id: str,
    ) -> Coordinator:
        """Build the trio. Selection is driven by the bus type — same
        signal that already drives scheduler selection upstream."""
        if isinstance(event_bus, InMemoryEventBus):
            return Coordinator(
                rate_limiter=InMemoryRateLimiter(),
                invalidation_bus=InMemoryInvalidationBus(),
                leader_elector=InMemoryLeaderElector(),
            )
        # Postgres backends are added in Task 15; until then the factory
        # falls through to in-memory even for the Postgres bus, with a
        # warning the lifespan logs at the call site.
        return Coordinator(
            rate_limiter=InMemoryRateLimiter(),
            invalidation_bus=InMemoryInvalidationBus(),
            leader_elector=InMemoryLeaderElector(),
        )
