"""Abstract base class for the platform event log store.

The event store holds the durable, append-only platform event log and
the per-subscription cursors that consume it. It is a sibling of
``Storage`` (JSONB entity metadata) and ``DocumentContentStore``
(document bodies) and shares the same backend connection/pool, which
is what lets an event append commit atomically with the write it
describes.

Spec: ``docs/superpowers/specs/2026-08-22-event-bus-design.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from primer.model.event import Event


class EventStore(ABC):
    """Append-only event log + per-subscriber cursors."""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """Create the events + cursors tables if absent. Idempotent."""

    @abstractmethod
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
        """Append one event; return its DB-assigned id.

        ``conn`` follows the ``Storage`` convention: when provided the
        insert rides the caller's open transaction so the event commits
        (or rolls back) atomically with the action it describes.
        Pool-less backends (SQLite) ignore it - the shared connection
        plus the write guard give the same atomicity.
        """

    @abstractmethod
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
        """Events with ``id > after_id`` in id order, up to ``limit``.

        The keyword filters are conjunctive and applied in SQL;
        richer matching (globs, field matchers, rego) belongs to the
        dispatcher's filter evaluator, not the store.
        """

    @abstractmethod
    async def max_id(self) -> int:
        """The highest event id, or 0 when the log is empty."""

    @abstractmethod
    async def prune(self, *, older_than: datetime, keep_after_id: int) -> int:
        """Delete events older than ``older_than`` AND ``id <= keep_after_id``.

        Returns the number of rows removed. ``keep_after_id`` is the
        floor of every active subscription cursor, so retention never
        eats rows a live subscriber has not consumed.
        """

    # -- cursors ----------------------------------------------------------

    @abstractmethod
    async def get_cursor(self, subscriber_id: str) -> int:
        """Last consumed event id for ``subscriber_id`` (0 when new)."""

    @abstractmethod
    async def set_cursor(self, subscriber_id: str, event_id: int) -> None:
        """Persist ``subscriber_id``'s cursor at ``event_id``."""

    @abstractmethod
    async def delete_cursor(self, subscriber_id: str) -> None:
        """Forget ``subscriber_id``'s cursor. Idempotent."""

    @abstractmethod
    async def active_cursor_floor(self) -> int | None:
        """The smallest stored cursor, or ``None`` when no cursors exist."""
