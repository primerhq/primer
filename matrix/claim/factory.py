"""Factory that builds a :class:`ClaimEngine` matching the bus type.

In-memory bus  → InMemoryClaimEngine  (single-process, zero-config).
Any other bus  → PostgresClaimEngine  (distributed, Postgres-backed leases).

Selection mirrors :class:`matrix.coordinator.factory.CoordinatorFactory` so a
single runtime-mode choice configures the whole stack consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from matrix.bus.in_memory import InMemoryEventBus
from matrix.claim.adapters.chats import ChatClaimAdapter
from matrix.claim.adapters.harnesses import HarnessClaimAdapter
from matrix.claim.adapters.sessions import SessionClaimAdapter
from matrix.claim.in_memory import InMemoryClaimEngine
from matrix.int.claim import ClaimKind
from matrix.int.event_bus import EventBus

if TYPE_CHECKING:
    from matrix.int.claim import ClaimEngine
    from matrix.int.storage_provider import StorageProvider


class ClaimEngineFactory:
    @staticmethod
    def create(
        *,
        storage_provider: "StorageProvider",
        event_bus: EventBus,
    ) -> "ClaimEngine":
        """Build a ClaimEngine + the three standard adapters.

        The bus type drives the selection:
        - :class:`~matrix.bus.in_memory.InMemoryEventBus` → in-memory engine
          (no Postgres pool required).
        - Any other bus → :class:`~matrix.claim.postgres.PostgresClaimEngine`
          (requires ``storage_provider.pool`` and ``storage_provider.leases_table``).

        Adapters are constructed here so callers do not need to know about the
        individual adapter constructors.  Each adapter receives the
        ``Storage[T]`` handle from *storage_provider*.
        """
        from matrix.model.workspace_session import WorkspaceSession
        from matrix.model.chats import Chat
        from matrix.model.harness import Harness

        adapters = {
            ClaimKind.SESSION: SessionClaimAdapter(
                session_storage=storage_provider.get_storage(WorkspaceSession),
            ),
            ClaimKind.CHAT: ChatClaimAdapter(
                chat_storage=storage_provider.get_storage(Chat),
            ),
            ClaimKind.HARNESS: HarnessClaimAdapter(
                harness_storage=storage_provider.get_storage(Harness),
            ),
        }

        if isinstance(event_bus, InMemoryEventBus):
            return InMemoryClaimEngine(adapters=adapters)

        from matrix.claim.postgres import PostgresClaimEngine

        return PostgresClaimEngine(
            storage_provider=storage_provider,
            adapters=adapters,
        )
