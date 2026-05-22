"""Yielding-tools event bus implementations.

Exports:

* :class:`InMemoryEventBus` — single-process asyncio queue. Tests
  and dev mode use this; the production app picks
  :class:`PostgresEventBus` instead.
* :class:`PostgresEventBus` — backed by postgres ``LISTEN/NOTIFY``
  on the ``matrix_yield_events`` channel. Production default.
* :class:`YieldEventListener` — background task wired up at app
  startup. Subscribes to the bus and routes each event to the
  scheduler's :meth:`mark_resumable` for any parked session keyed
  on it.
* :class:`TimerScheduler` — background task that polls the
  ``sessions`` table for ``timer:*`` parks whose ``parked_until``
  is due and publishes empty events to wake them.
* :class:`TimeoutSweeper` — background task that catches non-timer
  parks (ask_user, watch_files, MCP tasks) whose ``parked_until``
  elapsed without a real event firing; publishes the
  ``__yield_timeout__`` marker payload.

See :mod:`matrix.int.event_bus` for the abstract interface and the
spec at ``docs/superpowers/specs/2026-05-22-yielding-tools-design.md``
§6 for the design.
"""

from matrix.bus.in_memory import InMemoryEventBus
from matrix.bus.listener import YieldEventListener
from matrix.bus.postgres import PostgresEventBus
from matrix.bus.scheduler_tasks import TimeoutSweeper, TimerScheduler


__all__ = [
    "InMemoryEventBus",
    "PostgresEventBus",
    "TimeoutSweeper",
    "TimerScheduler",
    "YieldEventListener",
]
