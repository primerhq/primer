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
import logging
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

logger = logging.getLogger(__name__)


@dataclass
class _SessionState:
    turn_no: int = 0
    status: SessionStatus = SessionStatus.RUNNING
    last_worker_id: str | None = None
    attempt_count: int = 0
    last_error: str | None = None


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
        self._workers: dict[str, _WorkerState] = {}
        # What each worker registered with, so a heartbeat can register a row a peer purged (see heartbeat_worker).
        self._registrations: dict[str, dict[str, Any]] = {}
        # Workers this process has put into its drain (drain_worker): their rows come back
        # DRAINING from a dead mark, not active (H-3 r1 minor 3).
        self._draining_here: set[str] = set()
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

    def mark_worker_dead_for_test(self, worker_id: str) -> None:
        """Flip a registered worker to ``dead``.

        Production marks workers dead via the reaper sweep once they stop
        heart-beating; this single-process impl has no timer, so tests use
        this seam to exercise dead-worker cleanup paths."""
        w = self._workers[worker_id]
        w.info = w.info.model_copy(update={"status": "dead"})

    def watch_cancel(self, worker_id: str) -> AsyncIterator[str]:
        """Test-only parallel of watch_ready, scoped to cancel events."""

        async def _iter() -> AsyncIterator[str]:
            queue = self._workers[worker_id].cancel_queue
            while True:
                yield await queue.get()

        return _iter()

    # ---- Worker membership ----------------------------------------------

    async def register_worker(
        self, *, worker_id: str, host: str, pid: int, capacity: int, label: str | None = None,
    ) -> None:
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._registrations[worker_id] = {"host": host, "pid": pid, "capacity": capacity, "label": label}
            self._workers[worker_id] = _WorkerState(
                info=WorkerInfo(
                    id=worker_id, host=host, pid=pid, capacity=capacity, label=label,
                    started_at=now, last_heartbeat=now, status="active",
                ),
            )

    async def report_worker_label(self, worker_id: str, *, label: str) -> None:
        async with self._lock:
            w = self._workers.get(worker_id)
            if w is not None:
                w.info = w.info.model_copy(update={"label": label})
            if worker_id in self._registrations:
                self._registrations[worker_id]["label"] = label

    async def heartbeat_worker(self, worker_id: str) -> None:
        """Stamp the liveness time. A row a peer marked ``dead`` becomes ``active`` again (a ``draining`` row stays
        draining) and a row a peer purged is registered again from what this worker registered with: a live worker
        must not disappear from the registry because its writes were slow once. A deregistered worker is not brought
        back (``deregister_worker`` forgets its registration)."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            w = self._workers.get(worker_id)
            if w is None:
                reg = self._registrations.get(worker_id)
                if reg is not None:
                    logger.warning("heartbeat_worker: the row of worker %s was purged; registering it again", worker_id)
                    self._workers[worker_id] = _WorkerState(
                        info=WorkerInfo(
                            id=worker_id, started_at=now, last_heartbeat=now,
                            status="draining" if worker_id in self._draining_here else "active", **reg,
                        ),
                    )
                return
            update: dict[str, Any] = {"last_heartbeat": now}
            if w.info.status == "dead":
                update["status"] = "draining" if worker_id in self._draining_here else "active"
                # The dead -> restored transition itself, so the log fires once per dead episode.
                logger.warning(
                    "heartbeat_worker: worker %s was marked dead; restored to %s",
                    worker_id, update["status"],
                )
            w.info = w.info.model_copy(update=update)

    async def report_worker_load(self, worker_id: str, *, in_flight: int) -> None:
        async with self._lock:
            w = self._workers.get(worker_id)
            if w is not None:
                w.info = w.info.model_copy(update={"in_flight": in_flight})

    async def drain_worker(self, worker_id: str) -> None:
        async with self._lock:
            self._draining_here.add(worker_id)
            w = self._workers.get(worker_id)
            if w is not None:
                w.info = w.info.model_copy(update={"status": "draining"})

    async def deregister_worker(self, worker_id: str, *, keep_registration: bool = False) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)
            if not keep_registration:
                self._registrations.pop(worker_id, None)

    async def purge_dead_workers(self) -> int:
        async with self._lock:
            dead = [
                wid for wid, w in self._workers.items()
                if w.info.status == "dead"
            ]
            for wid in dead:
                self._workers.pop(wid, None)
            return len(dead)

    async def list_workers(self) -> list[WorkerInfo]:
        async with self._lock:
            return [w.info for w in self._workers.values()]

    # ---- Enqueue --------------------------------------------------------

    async def enqueue(
        self, session_id: str, *, ready_at: datetime | None = None,
    ) -> None:
        # The claim engine owns runnable state (its leases carry next_attempt_at),
        # and nothing reads the ready queue (no consumer drives watch_ready). A
        # wake used to put the session id onto every worker's ready queue, which
        # grew unboundedly. It now only records the notify for the metric.
        # (Review 2026-11-23 finding 1.10.)
        async with self._lock:
            self._notify_received_total += len(self._workers)

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
        # The claim engine owns the runnable state (its leases), so the
        # scheduler's runnable depth is always 0: a wake no longer enqueues
        # onto the unread ready queue. (Review 2026-11-23 finding 1.10.)
        runnable = 0
        return {
            "primer_sessions_active": sessions_by_status,
            "primer_sessions_runnable_queue_depth": runnable,
            "primer_lease_expirations_total": self._lease_expirations_total,
            "primer_scheduler_notify_received_total": (
                self._notify_received_total
            ),
        }
