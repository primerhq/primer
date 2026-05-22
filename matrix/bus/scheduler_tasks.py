"""Background tasks: timer-driven event publishing + timeout sweeper.

Spec: ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md`` §6.3, §6.4.

These two tasks are the "publishers without external sources" — the
internal drivers that make the timer-based parks (sleep) wake on
schedule and that catch parks whose external event never fires
within the parked_until deadline.

* :class:`TimerScheduler` polls the sessions table for ``timer:*``
  parks whose ``parked_until`` is due (or close to due) and
  publishes an empty event on the bus for each. Wakes only the
  sleep tool's parks today; future timer-style yields can use the
  same prefix.
* :class:`TimeoutSweeper` catches non-timer parks (ask_user,
  watch_files, MCP tasks) whose deadline elapsed without their
  external event firing. Publishes the ``__yield_timeout__``
  marker payload so the resume hook produces a YieldTimeout result.

Both run on a single asyncio task per app. Errors are logged and
the loop continues — neither is critical-path on the happy flow
(real events from the bus + the post-flip session_ready NOTIFY do
the wake), but they're the safety net.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from matrix.int.event_bus import EventBus
from matrix.worker.yield_runtime import make_timeout_payload

if TYPE_CHECKING:
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler


logger = logging.getLogger(__name__)


# Default poll cadence in seconds. Tunable per-task on construction
# if e.g. a deployment wants a sweeper that runs every minute
# instead of every 30s.
DEFAULT_TIMER_POLL_SECONDS = 2.0
DEFAULT_SWEEPER_POLL_SECONDS = 30.0


class _BackgroundTask:
    """Tiny base class — owns the asyncio task lifecycle."""

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=self._name)

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

    async def _run(self) -> None:  # pragma: no cover — overridden
        raise NotImplementedError


class TimerScheduler(_BackgroundTask):
    """Publishes empty events for timer parks whose deadline is due.

    Wakes the ``sleep`` tool (and any future timer-style yields) by
    NOTIFY-ing the bus when the row's ``parked_until`` <= now. The
    bus listener then flips the parked row to resumable; the worker
    pool wakes via ``session_ready`` and resumes the turn.

    A single instance per app suffices because the listener's
    mark_resumable is idempotent.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler,
        poll_seconds: float = DEFAULT_TIMER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="yield-timer-scheduler")
        self._bus = bus
        self._scheduler = scheduler
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "yield-timer-scheduler: tick failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """One iteration: find due timer parks, publish events."""
        keys = await _find_due_timer_keys(self._scheduler)
        for event_key in keys:
            await self._bus.publish(event_key, payload={})


class TimeoutSweeper(_BackgroundTask):
    """Publishes timeout markers for parks past their deadline.

    Catches non-timer parks whose external event never fires.
    Publishes ``__yield_timeout__`` payload so the worker's resume
    classifier synthesises a :class:`YieldTimeout` for the tool's
    resume hook.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler,
        poll_seconds: float = DEFAULT_SWEEPER_POLL_SECONDS,
    ) -> None:
        super().__init__(name="yield-timeout-sweeper")
        self._bus = bus
        self._scheduler = scheduler
        self._poll = poll_seconds

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "yield-timeout-sweeper: tick failed: %s", exc,
                )
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """One iteration: find expired non-timer parks, publish."""
        keys = await _find_expired_non_timer_keys(self._scheduler)
        payload = make_timeout_payload()
        for event_key in keys:
            await self._bus.publish(event_key, payload=payload)


# ===========================================================================
# Scheduler-flavour lookup helpers
# ===========================================================================
#
# We avoid adding query methods to the Scheduler ABC because they
# don't generalise across backends (in-memory walks a dict;
# postgres runs SQL). Instead, dispatch on type inside this module
# so the abstract interface stays minimal.


async def _find_due_timer_keys(scheduler) -> list[str]:
    """Find ``timer:*`` parked event_keys whose deadline is due."""
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler

    now = datetime.now(timezone.utc)

    if isinstance(scheduler, InMemoryScheduler):
        keys: list[str] = []
        async with scheduler._lock:
            for sess in scheduler._sessions.values():
                if (
                    sess.parked_status == "parked"
                    and sess.parked_event_key is not None
                    and sess.parked_event_key.startswith("timer:")
                    and sess.parked_until is not None
                    and sess.parked_until <= now
                ):
                    keys.append(sess.parked_event_key)
        return keys

    if isinstance(scheduler, PostgresScheduler):
        sql = """
            SELECT data->>'parked_event_key' AS event_key
              FROM sessions
             WHERE data->>'parked_status' = 'parked'
               AND data->>'parked_event_key' LIKE 'timer:%'
               AND (data->>'parked_until')::timestamptz <= now()
             LIMIT 200
        """
        async with scheduler._storage.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [r["event_key"] for r in rows]

    return []


async def _find_expired_non_timer_keys(scheduler) -> list[str]:
    """Find non-``timer:`` parked event_keys whose deadline elapsed.

    These are the parks whose external event never fired — the
    sweeper publishes a timeout marker so the resume hook produces
    a YieldTimeout result.
    """
    from matrix.scheduler.in_memory import InMemoryScheduler
    from matrix.scheduler.postgres import PostgresScheduler

    now = datetime.now(timezone.utc)

    if isinstance(scheduler, InMemoryScheduler):
        keys: list[str] = []
        async with scheduler._lock:
            for sess in scheduler._sessions.values():
                if (
                    sess.parked_status == "parked"
                    and sess.parked_event_key is not None
                    and not sess.parked_event_key.startswith("timer:")
                    and sess.parked_until is not None
                    and sess.parked_until <= now
                ):
                    keys.append(sess.parked_event_key)
        return keys

    if isinstance(scheduler, PostgresScheduler):
        sql = """
            SELECT data->>'parked_event_key' AS event_key
              FROM sessions
             WHERE data->>'parked_status' = 'parked'
               AND data->>'parked_event_key' NOT LIKE 'timer:%'
               AND (data->>'parked_until')::timestamptz <= now()
             LIMIT 200
        """
        async with scheduler._storage.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [r["event_key"] for r in rows]

    return []


__all__ = [
    "DEFAULT_SWEEPER_POLL_SECONDS",
    "DEFAULT_TIMER_POLL_SECONDS",
    "TimeoutSweeper",
    "TimerScheduler",
]
