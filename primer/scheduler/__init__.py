"""Scheduler implementations + factory.

See matrix.int.Scheduler for the ABC. Two impls ship:

* :class:`InMemoryScheduler` — single-process / tests
* :class:`PostgresScheduler` — production (lease columns + LISTEN/NOTIFY)

Construct via :class:`SchedulerFactory` from a discriminated
:class:`matrix.model.scheduler.SchedulerProviderConfig`.
"""

from primer.scheduler.factory import SchedulerFactory
from primer.scheduler.in_memory import InMemoryScheduler

__all__ = ["InMemoryScheduler", "SchedulerFactory"]
