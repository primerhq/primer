"""Factory that builds a :class:`Coordinator` matching the bus type.

In-memory bus → in-memory coordinator (single mode).
Postgres bus → Postgres coordinator (distributed mode).

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
        from matrix.coordinator.postgres import (
            PostgresInvalidationBus,
            PostgresLeaderElector,
            PostgresRateLimiter,
        )
        return Coordinator(
            rate_limiter=PostgresRateLimiter(storage_provider, owner_id),
            invalidation_bus=PostgresInvalidationBus(event_bus),
            leader_elector=PostgresLeaderElector(storage_provider, owner_id),
        )
