"""Abstract interface for the yielding-tools event bus.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §6.

The event bus carries the resume signal for parked sessions. Sources
publish events keyed by the same ``event_key`` the yielded tool
stamped into the parked-state blob; the bus's subscribers (a
background listener inside each worker pool) react by calling the
scheduler's :meth:`mark_resumable` for any session parked on that
key.

Two implementations live in :mod:`matrix.bus.postgres` (production —
``LISTEN/NOTIFY``) and :mod:`matrix.bus.in_memory` (tests / single-
process dev). Both honour the interface here; production callers
inject the right one via the app lifespan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    """Single published event observed by subscribers.

    Attributes
    ----------
    event_key
        The routing key the parked session is waiting on. Conventional
        prefixes: ``timer:``, ``ask_user:``, ``watch:``, ``mcp_task:``.
    payload
        Source-supplied payload dict. May include the matrix-internal
        marker keys (``__yield_timeout__``, ``__yield_cancelled__``);
        the worker's resume classifier strips them per
        :mod:`matrix.worker.yield_runtime`.
    published_at
        UTC timestamp the event was published.
    """

    event_key: str
    payload: dict[str, Any]
    published_at: datetime


class EventBus(ABC):
    """Pluggable transport for yield resume events.

    Implementations connect the publishers (timer scheduler, API
    endpoints, MCP server bridges, local watchers) to the worker
    pool's resumable-flip path. The bus is one-way: subscribers
    react; no acks, no per-subscriber filtering beyond the listener
    deciding to ignore unmatched keys.

    The contract is small on purpose. Each implementation owns:
    * lifecycle (``initialize`` / ``aclose``),
    * ``publish`` semantics (fire-and-forget),
    * ``subscribe`` semantics (an async iterator of events the
      worker pool consumes).
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Open connections / resources. Idempotent."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release connections / resources. Idempotent."""

    @abstractmethod
    async def publish(
        self,
        event_key: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Publish ``payload`` under ``event_key``.

        Fire-and-forget. The bus does not guarantee delivery to
        any specific subscriber — it guarantees that the
        scheduler's ``mark_resumable`` will be invoked by SOME
        listener, on SOME worker, for the named ``event_key``,
        unless the listener is offline or the row is no longer
        parked.

        ``payload=None`` is shorthand for an empty dict.
        """

    @abstractmethod
    def subscribe(self) -> "EventSubscription":
        """Subscribe to ALL events on the bus.

        Returns an :class:`EventSubscription` async-iterator the
        caller iterates with ``async for``. The subscription owns
        its own queue / connection and must be ``aclose()``-d when
        the consumer is done.

        The bus implementation decides whether subscriptions are
        broadcast (every subscriber sees every event) or
        load-balanced (each event goes to exactly one). The default
        for both shipped impls is broadcast — the worker pool's
        resumable-flip is idempotent (``mark_resumable``'s atomic
        UPDATE-WITH-WHERE means duplicate flips are no-ops).
        """


class EventSubscription(ABC):
    """Async iterator wrapping a live subscription to the bus.

    Used as::

        sub = bus.subscribe()
        try:
            async for event in sub:
                handle(event)
        finally:
            await sub.aclose()
    """

    @abstractmethod
    def __aiter__(self) -> "EventSubscription":
        return self

    @abstractmethod
    async def __anext__(self) -> Event:
        """Return the next event; raise StopAsyncIteration on close."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the subscription. Idempotent. After aclose()
        further __anext__ calls raise StopAsyncIteration."""


__all__ = [
    "Event",
    "EventBus",
    "EventSubscription",
]
