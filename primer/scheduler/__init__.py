"""Scheduler implementations + factory.

See primer.int.Scheduler for the ABC. Two impls ship:

* :class:`InMemoryScheduler` — single-process / tests
* :class:`PostgresScheduler` — production (lease columns + LISTEN/NOTIFY)

Construct via :class:`SchedulerFactory` from a discriminated
:class:`primer.model.scheduler.SchedulerProviderConfig`.
"""

from primer.scheduler.factory import SchedulerFactory
from primer.scheduler.in_memory import InMemoryScheduler

__all__ = ["InMemoryScheduler", "SchedulerFactory"]
