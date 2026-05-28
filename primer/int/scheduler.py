"""Distributed session scheduler — ABC + supporting value types.

The :class:`Scheduler` coordinates which worker process runs which
session. The contract is intentionally narrow: enqueue, session-lease
lifecycle (complete_turn / park_turn / clear_park / mark_resumable),
and signalling. Claim / heartbeat / release for sessions, chats, and
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
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from primer.model.workspace_session import SessionStatus


class Lease(BaseModel):
    """Snapshot of a successful session claim.

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

    # ---- Park / resume (yielding-tool feature) ---------------------------

    @abstractmethod
    async def park_turn(
        self,
        worker_id: str,
        session_id: str,
        *,
        expected_turn_no: int,
        parked_event_key: str,
        parked_until: "datetime",
        parked_at: "datetime",
        parked_state: dict[str, Any],
    ) -> CompleteTurnResult:
        """Park the in-flight turn instead of completing it.

        Writes the parked-state blob into the session row, releases
        the worker's lease, and does NOT advance ``turn_no`` — the
        same turn resumes when an event flips ``parked_status`` to
        ``'resumable'``. Used by the yielding-tools feature (see
        spec §7.2).

        Returns the same outcome enum as :meth:`complete_turn`:

        * SUCCESS — park written, lease released; another worker
          will pick this session up when it becomes resumable.
        * LEASE_LOST — another worker stole the lease before we
          parked; the park was NOT written. Caller treats as a
          turn that should not be re-run (the other worker is
          already running it).
        * TURN_CONFLICT — ``expected_turn_no`` no longer matches
          the row's ``turn_no``; same response as LEASE_LOST.
        """

    @abstractmethod
    async def mark_resumable(
        self,
        event_key: str,
        *,
        resume_event_payload: dict[str, Any],
    ) -> int:
        """Flip parked session(s) keyed on ``event_key`` to resumable.

        Called by the event bus listener (M2) and the timeout
        sweeper / cancel-yielded-tool API. Atomic per row: only the
        first publisher to flip a given parked row wins (subsequent
        calls see ``parked_status != 'parked'`` and no-op). Returns
        the number of rows that were flipped (typically 0 or 1 —
        higher only if multiple sessions happen to share the same
        event_key, which is unusual but supported).

        After the flip the scheduler also re-arms the lease and
        notifies the ``session_ready`` channel so the worker pool
        wakes immediately.
        """

    @abstractmethod
    async def clear_park(self, session_id: str) -> None:
        """NULL every parked_* column on the session row.

        Called by the worker's resume path AFTER the resume hook
        has produced its result and the synthesised tool_result
        message has been persisted to history. Once cleared the
        row looks like a normal non-parked session again to the
        claim path; a subsequent claim picks it up as a fresh turn.

        Idempotent: clearing an already-clear row (or a row that
        never existed) is a silent no-op — the worker doesn't
        need to gate on row existence, and a concurrent storage
        delete can't surface as a 5xx through this path.
        """

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
