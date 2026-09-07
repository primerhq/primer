"""Factory that builds a :class:`ClaimEngine` matching the bus type.

In-memory bus  → InMemoryClaimEngine  (single-process, zero-config).
Any other bus  → PostgresClaimEngine  (distributed, Postgres-backed leases).

Selection mirrors :class:`primer.coordinator.factory.CoordinatorFactory` so a
single runtime-mode choice configures the whole stack consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.int.event_bus import EventBus

if TYPE_CHECKING:
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.int.claim import ClaimEngine
    from primer.int.storage_provider import StorageProvider


class ClaimEngineFactory:
    @staticmethod
    def create(
        *,
        storage_provider: "StorageProvider",
        event_bus: EventBus,
        workspace_registry: "WorkspaceRegistry | None" = None,
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

        ``workspace_registry`` (01a068ea) activates
        :class:`SessionClaimAdapter`'s terminal-error write: every real
        caller already builds a registry for its own turn-execution path,
        so this is always the SAME registry, not a second one. Optional
        (``None`` degrades exactly like the pre-01a068ea default: no
        terminal-error write) so callers that only exercise the
        harness/trigger adapters don't need one.
        """
        from primer.model.workspace_session import WorkspaceSession
        from primer.model.harness import Harness
        from primer.model.tool_call_task import ToolCallTask
        from primer.model.trigger import Trigger

        adapters = {
            ClaimKind.SESSION: SessionClaimAdapter(
                session_storage=storage_provider.get_storage(WorkspaceSession),
                workspace_registry=workspace_registry,
                event_bus=event_bus,
            ),
            ClaimKind.HARNESS: HarnessClaimAdapter(
                harness_storage=storage_provider.get_storage(Harness),
            ),
            ClaimKind.TRIGGER: TriggerClaimAdapter(
                storage=storage_provider.get_storage(Trigger),
            ),
            ClaimKind.TOOL_CALL: ToolCallClaimAdapter(
                task_storage=storage_provider.get_storage(ToolCallTask),
            ),
        }

        if isinstance(event_bus, InMemoryEventBus):
            engine: "ClaimEngine" = InMemoryClaimEngine(adapters=adapters)
        else:
            from primer.claim.postgres import PostgresClaimEngine

            engine = PostgresClaimEngine(
                storage_provider=storage_provider,
                adapters=adapters,
            )

        # 01a0518b (mixed-park wake seam, hazard-fix ruling): the engine
        # itself never imports worker-layer code (durably_mark_session_
        # resumable lives in primer.session.yields) - the factory closes
        # over the session storage + this SAME engine and hands the
        # engine only a plain callable, invoked strictly AFTER a
        # release's own transaction commits (see PostReleaseWake's own
        # docstring for why an adapter can't call this directly).
        session_storage = storage_provider.get_storage(WorkspaceSession)

        async def _wake_session_on_tool_wait_ready(signal) -> None:
            from primer.session.yields import durably_mark_session_resumable

            session = await session_storage.get(signal.session_id)
            if session is None:
                return
            await durably_mark_session_resumable(
                session,
                event_key=signal.event_key,
                payload=signal.payload,
                session_storage=session_storage,
                engine=engine,
            )

        engine.bind_post_release_hook(_wake_session_on_tool_wait_ready)
        return engine
