"""Distributed session scheduler — ABC + supporting value types.

The :class:`Scheduler` coordinates which worker process runs which
session. The contract is intentionally narrow: enqueue, claim, lease
heartbeat, atomic turn completion, plus a best-effort wake-up channel.

Two impls ship: :class:`matrix.scheduler.PostgresScheduler` for
production (lease columns + ``SELECT … FOR UPDATE SKIP LOCKED`` +
``LISTEN/NOTIFY``); :class:`matrix.scheduler.InMemoryScheduler` for
tests and single-process dev.

See ``docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md``
for the full design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from matrix.model.session import SessionStatus


class Lease(BaseModel):
    """Snapshot of a successful claim.

    The worker uses ``turn_no`` as the fence token: the eventual
    ``complete_turn`` call passes ``expected_turn_no=lease.turn_no``,
    and the scheduler accepts only if the persisted value still matches.
    """

    session_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    expires_at: datetime
    attempt_count: int = Field(..., ge=0)
    turn_no: int = Field(..., ge=0)


class WorkerInfo(BaseModel):
    """Membership record for a registered worker."""

    id: str = Field(..., min_length=1)
    host: str
    pid: int
    capacity: int = Field(..., ge=1)
    started_at: datetime
    last_heartbeat: datetime
    status: Literal["active", "draining", "dead"]


class FailureRecord(BaseModel):
    """Optional payload for ``Scheduler.complete_turn`` on failure paths.

    When supplied the scheduler writes ``error_text`` into
    ``Session.last_error`` and ``attempt_count`` into
    ``Session.attempt_count`` in the same transaction as the lease
    release.
    """

    error_text: str
    attempt_count: int = Field(..., ge=0)


class CompleteTurnResult(str, Enum):
    SUCCESS = "success"
    LEASE_LOST = "lease_lost"
    TURN_CONFLICT = "turn_conflict"


class Scheduler(ABC):
    """Coordinator: who runs which session, when."""

    @abstractmethod
    async def initialize(self) -> None:
        """One-time setup (e.g. create scheduler-internal tables)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release any held resources (LISTEN connections, asyncio tasks)."""

    # ---- Worker membership -----------------------------------------------

    @abstractmethod
    async def register_worker(
        self,
        *,
        worker_id: str,
        host: str,
        pid: int,
        capacity: int,
    ) -> None: ...

    @abstractmethod
    async def heartbeat_worker(self, worker_id: str) -> None: ...

    @abstractmethod
    async def drain_worker(self, worker_id: str) -> None: ...

    @abstractmethod
    async def deregister_worker(self, worker_id: str) -> None: ...

    @abstractmethod
    async def list_workers(self) -> list[WorkerInfo]: ...

    # ---- Enqueue ---------------------------------------------------------

    @abstractmethod
    async def enqueue(
        self,
        session_id: str,
        *,
        ready_at: datetime | None = None,
    ) -> None:
        """Mark a session runnable and best-effort wake any idle worker."""

    # ---- Claim + lease ---------------------------------------------------

    @abstractmethod
    async def claim(
        self,
        worker_id: str,
        *,
        max_count: int = 1,
    ) -> list[Lease]: ...

    @abstractmethod
    async def heartbeat_leases(
        self,
        worker_id: str,
        session_ids: Sequence[str],
    ) -> list[str]:
        """Extend lease TTLs. Returns the subset still owned by us."""

    # ---- Atomic turn completion ------------------------------------------

    @abstractmethod
    async def complete_turn(
        self,
        worker_id: str,
        session_id: str,
        *,
        expected_turn_no: int,
        new_status: SessionStatus,
        ended_reason: str | None = None,
        re_enqueue: bool,
        backoff: timedelta | None = None,
        record_failure: FailureRecord | None = None,
    ) -> CompleteTurnResult: ...

    # ---- Hints + cancel --------------------------------------------------

    @abstractmethod
    def watch_ready(self, worker_id: str) -> AsyncIterator[str]:
        """Yields session_ids that just became runnable. Best-effort hint."""

    @abstractmethod
    async def signal_cancel(self, session_id: str) -> None:
        """Best-effort: notify the worker currently holding ``session_id``
        to cancel its in-flight turn."""

    # ---- Metrics ---------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of scheduler metrics. Default: empty dict.

        Subclasses override to expose real counters/gauges. See spec §14.
        Sync-only by design: callers may invoke this from any context
        (HTTP handler, Prometheus scrape) without juggling event loops.
        Implementations that need DB-side aggregates should expose those
        via a separate ``async`` method (see
        :meth:`PostgresScheduler.metrics_db_snapshot`)."""
        return {}


__all__ = [
    "CompleteTurnResult",
    "FailureRecord",
    "Lease",
    "Scheduler",
    "WorkerInfo",
]
