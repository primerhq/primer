"""In-memory :class:`Scheduler` for tests + single-process dev.

Not safe for multi-process deployment — there is no cross-process
synchronisation. Tests parametrise across this and the Postgres impl
to keep behaviour aligned.

Maintains a parallel ``_sessions`` dict so tests can seed sessions
without touching ``Storage[Session]`` (production reads from there;
this single-process impl is self-contained).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.int.scheduler import (
    Scheduler,
    WorkerInfo,
)
from primer.model.workspace_session import SessionStatus

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


@dataclass
class _SessionState:
    turn_no: int = 0
    status: SessionStatus = SessionStatus.RUNNING
    last_worker_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None


@dataclass
class _LeaseState:
    worker_id: str | None = None
    expires_at: datetime | None = None
    next_attempt_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    runnable: bool = False


@dataclass
class _WorkerState:
    info: WorkerInfo
    ready_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    cancel_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)


class InMemoryScheduler(Scheduler):
    """Single-process scheduler. NOT safe for multi-process deployment."""

    def __init__(
        self,
        *,
        storage_provider: "StorageProvider | None" = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._leases: dict[str, _LeaseState] = {}
        self._workers: dict[str, _WorkerState] = {}
        self.lease_ttl_seconds: int = 30  # mutable; WorkerPool sets at start
        self._storage = storage_provider
        # ---- metrics (spec §14) ----
        self._notify_received_total: int = 0
        self._lease_expirations_total: int = 0

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    # ---- Test seam ------------------------------------------------------

    def register_session_for_test(
        self,
        sid: str,
        *,
        turn_no: int = 0,
        status: SessionStatus = SessionStatus.RUNNING,
    ) -> None:
        """Seed a synthetic session row. Tests use this instead of
        going through Storage[Session]."""
        self._sessions[sid] = _SessionState(turn_no=turn_no, status=status)

    def session_snapshot_for_test(self, sid: str) -> _SessionState:
        return self._sessions[sid]

    def watch_cancel(self, worker_id: str) -> AsyncIterator[str]:
        """Test-only parallel of watch_ready, scoped to cancel events."""

        async def _iter() -> AsyncIterator[str]:
            queue = self._workers[worker_id].cancel_queue
            while True:
                yield await queue.get()

        return _iter()

    # ---- Worker membership ----------------------------------------------

    async def register_worker(
        self, *, worker_id: str, host: str, pid: int, capacity: int,
    ) -> None:
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._workers[worker_id] = _WorkerState(
                info=WorkerInfo(
                    id=worker_id, host=host, pid=pid, capacity=capacity,
                    started_at=now, last_heartbeat=now, status="active",
                ),
            )

    async def heartbeat_worker(self, worker_id: str) -> None:
        async with self._lock:
            w = self._workers.get(worker_id)
            if w is not None:
                w.info = w.info.model_copy(
                    update={"last_heartbeat": datetime.now(timezone.utc)},
                )

    async def drain_worker(self, worker_id: str) -> None:
        async with self._lock:
            w = self._workers.get(worker_id)
            if w is not None:
                w.info = w.info.model_copy(update={"status": "draining"})

    async def deregister_worker(self, worker_id: str) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)

    async def list_workers(self) -> list[WorkerInfo]:
        async with self._lock:
            return [w.info for w in self._workers.values()]

    # ---- Enqueue --------------------------------------------------------

    async def enqueue(
        self, session_id: str, *, ready_at: datetime | None = None,
    ) -> None:
        async with self._lock:
            lease = self._leases.setdefault(session_id, _LeaseState())
            lease.runnable = True
            if ready_at is not None:
                lease.next_attempt_at = ready_at
            for w in self._workers.values():
                w.ready_queue.put_nowait(session_id)
                self._notify_received_total += 1

    # ---- Hints + cancel -------------------------------------------------

    def watch_ready(self, worker_id: str) -> AsyncIterator[str]:
        async def _iter() -> AsyncIterator[str]:
            queue = self._workers[worker_id].ready_queue
            while True:
                yield await queue.get()

        return _iter()

    async def signal_cancel(self, session_id: str) -> None:
        async with self._lock:
            for w in self._workers.values():
                w.cancel_queue.put_nowait(session_id)

    # ---- Metrics --------------------------------------------------------

    def metrics_snapshot(self) -> dict[str, Any]:
        """Snapshot of in-process scheduler metrics. See spec §14.

        Weak consistency: this is a passive read (no lock held), so a
        concurrent ``enqueue`` may add or remove a
        session between the two iterations. Acceptable per spec §3
        ("everything else tolerates weak consistency")."""
        sessions_by_status: dict[str, int] = {}
        for s in self._sessions.values():
            key = s.status.value
            sessions_by_status[key] = sessions_by_status.get(key, 0) + 1
        runnable = sum(
            1 for lease in self._leases.values()
            if lease.runnable and lease.worker_id is None
        )
        return {
            "primer_sessions_active": sessions_by_status,
            "primer_sessions_runnable_queue_depth": runnable,
            "primer_lease_expirations_total": self._lease_expirations_total,
            "primer_scheduler_notify_received_total": (
                self._notify_received_total
            ),
        }
