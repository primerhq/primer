"""Background listener that flips parked sessions to resumable.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §6.2.

The listener subscribes to the event bus and, for each event,
invokes the scheduler's :meth:`mark_resumable` for every parked
session keyed on that ``event_key``. The scheduler's atomic
``UPDATE WHERE parked_status='parked' AND parked_event_key=...``
guards against double-publish (only the first publisher wins).

One listener per app is sufficient — broadcast LISTEN/NOTIFY means
every app sees every event, but the atomic flip means only one wins.
The post-flip ``pg_notify('session_ready')`` wakes the worker pool
which then claims and resumes.
"""

from __future__ import annotations

import asyncio
import logging

from primer.int.event_bus import EventBus
from primer.int.scheduler import Scheduler


logger = logging.getLogger(__name__)


class YieldEventListener:
    """Background task: subscribe to the event bus, flip parked rows.

    Lifecycle:

    * ``start()`` — schedules an asyncio task that runs the
      listener loop. Returns immediately.
    * ``stop()`` — cancels the task and awaits its exit. Idempotent.

    Wire into the app lifespan: start at startup, stop at shutdown.
    """

    def __init__(self, *, bus: EventBus, scheduler: Scheduler) -> None:
        self._bus = bus
        self._scheduler = scheduler
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="yield-event-listener",
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    async def _run(self) -> None:
        sub = self._bus.subscribe()
        try:
            async for event in sub:
                if self._stopping:
                    break
                await self._handle_event(event)
        finally:
            await sub.aclose()

    async def _handle_event(self, event) -> None:
        """For each event_key, mark every parked session resumable.

        ``mark_resumable`` returns the number of rows flipped (0 if
        no matching parked row exists — common, the bus is broadcast
        so every app sees every event). Errors are logged but do
        not break the listener; the loop continues with the next
        event.
        """
        try:
            n = await self._scheduler.mark_resumable(
                event.event_key,
                resume_event_payload=event.payload,
            )
            if n > 0:
                logger.info(
                    "yield-event-listener: flipped %d session(s) for "
                    "event_key=%r",
                    n, event.event_key,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "yield-event-listener: mark_resumable failed for "
                "event_key=%r: %s",
                event.event_key, exc,
            )


__all__ = ["YieldEventListener"]
