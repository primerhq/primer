"""Distributed session scheduler — ABC + supporting value types.

The :class:`Scheduler` coordinates which worker process runs which
session. The contract is intentionally narrow: worker membership,
enqueue, and signalling. Claim / heartbeat / release plus the session
turn lifecycle (complete / park / resume) for sessions, chats, and
harnesses have been replaced by the polymorphic :class:`ClaimEngine`.

Two impls ship: :class:`primer.scheduler.PostgresScheduler` for
production; :class:`primer.scheduler.InMemoryScheduler` for tests and
single-process dev.

See ``docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md``
for the full design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Lease(BaseModel):
    """Snapshot of a successful session claim.

    Used for worker-lifecycle bookkeeping: the worker presents the lease
    during heartbeat calls and holds ``turn_no`` as the fence token for
    scheduler-side conflict detection.
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
    """Failure-bookkeeping payload retained for serialization compatibility.

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
