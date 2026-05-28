"""Factory that builds a :class:`ClaimEngine` matching the bus type.

In-memory bus  → InMemoryClaimEngine  (single-process, zero-config).
Any other bus  → PostgresClaimEngine  (distributed, Postgres-backed leases).

Selection mirrors :class:`primer.coordinator.factory.CoordinatorFactory` so a
single runtime-mode choice configures the whole stack consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.chats import ChatClaimAdapter
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.int.event_bus import EventBus

if TYPE_CHECKING:
    from primer.int.claim import ClaimEngine
    from primer.int.storage_provider import StorageProvider


class ClaimEngineFactory:
    @staticmethod
    def create(
        *,
        storage_provider: "StorageProvider",
        event_bus: EventBus,
    ) -> "ClaimEngine":
        """Build a ClaimEngine + the three standard adapters.

        The bus type drives the selection:
        - :class:`~primer.bus.in_memory.InMemoryEventBus` → in-memory engine
          (no Postgres pool required).
        - Any other bus → :class:`~primer.claim.postgres.PostgresClaimEngine`
          (requires ``storage_provider.pool`` and ``storage_provider.leases_table``).

        Adapters are constructed here so callers do not need to know about the
        individual adapter constructors.  Each adapter receives the
        ``Storage[T]`` handle from *storage_provider*.
        """
        from primer.model.workspace_session import WorkspaceSession
        from primer.model.chats import Chat
        from primer.model.harness import Harness

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

        from primer.claim.postgres import PostgresClaimEngine

        return PostgresClaimEngine(
            storage_provider=storage_provider,
            adapters=adapters,
        )
