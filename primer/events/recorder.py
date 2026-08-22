"""EventRecorder: the single seam action sites emit through.

Storage-layer CRUD emission is automatic; every other action calls
``recorder.emit(...)``. Two contracts, chosen by the ``conn`` kwarg:

* ``conn=None`` (the default): the append gets its own short
  transaction and NEVER raises out of ``emit`` - an action must not
  fail because the event log hiccuped. Failures are logged.
* ``conn`` passed: the caller wants the event atomic with its own
  open transaction, so errors propagate (atomicity is the point).

After a successful append the recorder publishes a best-effort
``events_appended`` hint on the wake bus so the dispatcher picks the
rows up without waiting for its poll interval.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.events.catalog import is_known_event_type

if TYPE_CHECKING:
    from primer.int.event_bus import EventBus
    from primer.int.event_store import EventStore

logger = logging.getLogger(__name__)

EVENTS_APPENDED_KEY = "events_appended"


class EventRecorder:
    def __init__(
        self,
        event_store: "EventStore | None",
        bus: "EventBus | None" = None,
    ) -> None:
        self._store = event_store
        self._bus = bus

    async def emit(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
        entity_kind: str | None = None,
        entity_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        conn: Any | None = None,
    ) -> int | None:
        """Append one action event; return its id (None on swallowed
        failure). Unknown ``event_type`` raises ValueError always - a
        typo is a programming error, not an operational hiccup."""
        if not is_known_event_type(event_type):
            raise ValueError(
                f"unknown event type {event_type!r}: add it to "
                "primer/events/catalog.py or register the entity kind"
            )
        if self._store is None:
            # Store-less harness (duck-typed test provider): the type
            # check above still ran, the append is skipped.
            return None
        try:
            event_id = await self._store.append(
                event_type=event_type,
                actor=actor,
                payload=payload,
                entity_kind=entity_kind,
                entity_id=entity_id,
                workspace_id=workspace_id,
                session_id=session_id,
                correlation_id=correlation_id,
                conn=conn,
            )
        except Exception:
            if conn is not None:
                raise
            logger.exception("event %s dropped: append failed", event_type)
            return None
        await self.hint(event_id)
        return event_id

    async def hint(self, max_id: int | None = None) -> None:
        """Best-effort 'new rows exist' nudge on the wake bus."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(
                EVENTS_APPENDED_KEY,
                {"max_id": max_id} if max_id is not None else {},
            )
        except Exception:  # noqa: BLE001
            logger.warning("events_appended hint failed", exc_info=True)


def recorder_for(storage_provider: Any, bus: Any = None) -> EventRecorder:
    """Tolerant factory: a provider without ``get_event_store`` (duck-
    typed fakes in older test harnesses) yields a store-less recorder
    whose emits validate and then no-op."""
    getter = getattr(storage_provider, "get_event_store", None)
    store = getter() if callable(getter) else None
    return EventRecorder(store, bus)


def actor_of(ref: Any) -> str:
    """Compact actor string for the event envelope.

    Accepts a PrincipalRef-shaped object (``type`` + ``id``) or None;
    anything else degrades to "system" rather than raising.
    """
    ref_type = getattr(ref, "type", None)
    ref_id = getattr(ref, "id", None)
    if ref_type and ref_id:
        return f"{ref_type}:{ref_id}"
    return "system"


__all__ = ["EventRecorder", "EVENTS_APPENDED_KEY", "actor_of", "recorder_for"]
