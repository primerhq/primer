"""In-memory :class:`EventStore` for tests and bus-less harnesses.

Pairs with the in-memory storage fakes the unit suites run: same
contract as the SQLite/Postgres stores, no durability. ``conn`` is
accepted and ignored (pool-less parity, like the SQLite store).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from primer.int.event_store import EventStore
from primer.model.event import Event


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._cursors: dict[str, int] = {}
        self._next_id = 1

    async def ensure_schema(self) -> None:
        return

    async def append(
        self,
        *,
        event_type: str,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        entity_kind: str | None = None,
        entity_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
        conn: Any | None = None,
    ) -> int:
        del conn
        event = Event(
            id=self._next_id,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            actor=actor,
            entity_kind=entity_kind,
            entity_id=entity_id,
            workspace_id=workspace_id,
            session_id=session_id,
            correlation_id=correlation_id,
            payload=dict(payload or {}),
        )
        self._events.append(event)
        self._next_id += 1
        return event.id

    async def read_after(
        self,
        after_id: int,
        *,
        limit: int = 200,
        event_type_prefix: str | None = None,
        entity_kind: str | None = None,
        entity_id: str | None = None,
        workspace_id: str | None = None,
        since: datetime | None = None,
    ) -> list[Event]:
        out: list[Event] = []
        for e in self._events:
            if e.id <= after_id:
                continue
            if event_type_prefix and not e.event_type.startswith(
                event_type_prefix,
            ):
                continue
            if entity_kind is not None and e.entity_kind != entity_kind:
                continue
            if entity_id is not None and e.entity_id != entity_id:
                continue
            if workspace_id is not None and e.workspace_id != workspace_id:
                continue
            if since is not None and e.occurred_at < since:
                continue
            out.append(e)
            if len(out) >= max(0, limit):
                break
        return out

    async def max_id(self) -> int:
        return self._events[-1].id if self._events else 0

    async def prune(self, *, older_than: datetime, keep_after_id: int) -> int:
        before = len(self._events)
        self._events = [
            e for e in self._events
            if not (e.occurred_at < older_than and e.id <= keep_after_id)
        ]
        return before - len(self._events)

    async def get_cursor(self, subscriber_id: str) -> int:
        return self._cursors.get(subscriber_id, 0)

    async def set_cursor(self, subscriber_id: str, event_id: int) -> None:
        self._cursors[subscriber_id] = event_id

    async def delete_cursor(self, subscriber_id: str) -> None:
        self._cursors.pop(subscriber_id, None)

    async def active_cursor_floor(self) -> int | None:
        return min(self._cursors.values()) if self._cursors else None


__all__ = ["InMemoryEventStore"]
