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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from matrix.int.scheduler import (
    _DEFAULT_HEARTBEAT_STALE_AFTER,
    ChatLease,
    CompleteTurnResult,
    FailureRecord,
    Lease,
    Scheduler,
    WorkerInfo,
)
from matrix.model.chats import Chat
from matrix.model.session import SessionStatus
from matrix.model.storage import OffsetPage

if TYPE_CHECKING:
    from matrix.int.storage_provider import StorageProvider


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

    # ---- Claim + lease --------------------------------------------------

    async def claim(
        self, worker_id: str, *, max_count: int = 1,
    ) -> list[Lease]:
        async with self._lock:
            now = datetime.now(timezone.utc)
            picked: list[Lease] = []
            for sid, lease in self._leases.items():
                if len(picked) >= max_count:
                    break
                if not lease.runnable:
                    continue
                if lease.next_attempt_at > now:
                    continue
                if lease.worker_id is not None and (
                    lease.expires_at is not None and lease.expires_at > now
                ):
                    continue
                # Lease was held by another worker but expired — count
                # as an expiration event before we steal it.
                if (
                    lease.worker_id is not None
                    and lease.expires_at is not None
                    and lease.expires_at < now
                ):
                    self._lease_expirations_total += 1
                session = self._sessions.get(sid)
                if session is None:
                    continue
                lease.worker_id = worker_id
                lease.expires_at = now + timedelta(
                    seconds=self.lease_ttl_seconds
                )
                picked.append(Lease(
                    session_id=sid,
                    worker_id=worker_id,
                    expires_at=lease.expires_at,
                    attempt_count=session.attempt_count,
                    turn_no=session.turn_no,
                ))
            return picked

    async def heartbeat_leases(
        self, worker_id: str, session_ids: Sequence[str],
    ) -> list[str]:
        async with self._lock:
            now = datetime.now(timezone.utc)
            still_owned: list[str] = []
            for sid in session_ids:
                lease = self._leases.get(sid)
                if lease is None or lease.worker_id != worker_id:
                    continue
                lease.expires_at = now + timedelta(
                    seconds=self.lease_ttl_seconds
                )
                still_owned.append(sid)
            return still_owned

    # ---- Atomic turn completion -----------------------------------------

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
    ) -> CompleteTurnResult:
        async with self._lock:
            session = self._sessions.get(session_id)
            lease = self._leases.get(session_id)
            if session is None or lease is None:
                return CompleteTurnResult.LEASE_LOST
            if lease.worker_id != worker_id:
                return CompleteTurnResult.LEASE_LOST
            if session.turn_no != expected_turn_no:
                return CompleteTurnResult.TURN_CONFLICT

            session.turn_no += 1
            session.status = new_status
            session.last_worker_id = worker_id
            if record_failure is not None:
                session.attempt_count = record_failure.attempt_count
                session.last_error = record_failure.error_text
            else:
                session.attempt_count = 0
                session.last_error = None

            lease.worker_id = None
            lease.expires_at = None
            lease.runnable = re_enqueue
            now = datetime.now(timezone.utc)
            if backoff is not None:
                lease.next_attempt_at = now + backoff
            else:
                lease.next_attempt_at = now

            if re_enqueue:
                for w in self._workers.values():
                    w.ready_queue.put_nowait(session_id)
                    self._notify_received_total += 1
            return CompleteTurnResult.SUCCESS

    # ---- Park / resume (yielding-tools feature) -------------------------

    async def park_turn(
        self,
        worker_id: str,
        session_id: str,
        *,
        expected_turn_no: int,
        parked_event_key: str,
        parked_until,
        parked_at,
        parked_state: dict[str, Any],
    ) -> CompleteTurnResult:
        """Park the in-flight turn without advancing turn_no.

        Mirrors :meth:`PostgresScheduler.park_turn` for in-process
        tests. The Session model carries the parked_* fields
        natively (added by the yielding-tools M1 migration), so we
        write them via attribute assignment.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            lease = self._leases.get(session_id)
            if session is None or lease is None:
                return CompleteTurnResult.LEASE_LOST
            if lease.worker_id != worker_id:
                return CompleteTurnResult.LEASE_LOST
            if session.turn_no != expected_turn_no:
                return CompleteTurnResult.TURN_CONFLICT

            session.parked_status = "parked"
            session.parked_event_key = parked_event_key
            session.parked_until = parked_until
            session.parked_at = parked_at
            session.parked_state = parked_state

            lease.worker_id = None
            lease.expires_at = None
            lease.runnable = False
            lease.next_attempt_at = datetime.now(timezone.utc)
            return CompleteTurnResult.SUCCESS

    async def clear_park(self, session_id: str) -> None:
        """NULL every parked_* column on the session row.

        Idempotent: unknown session id or already-cleared row is
        a silent no-op (mirrors PostgresScheduler.clear_park).
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.parked_status = None
            session.parked_event_key = None
            session.parked_until = None
            session.parked_at = None
            session.parked_state = None

    async def mark_resumable(
        self,
        event_key: str,
        *,
        resume_event_payload: dict[str, Any],
    ) -> int:
        """Flip parked sessions keyed on ``event_key`` to resumable.

        Walks the in-memory session table for matching parked rows;
        each is atomically flipped under the scheduler lock so only
        the first publisher wins per row.
        """
        flipped = 0
        async with self._lock:
            for sid, session in self._sessions.items():
                if (
                    session.parked_status != "parked"
                    or session.parked_event_key != event_key
                ):
                    continue
                lease = self._leases.get(sid)
                if lease is None:
                    continue

                session.parked_status = "resumable"
                state = dict(session.parked_state or {})
                state["resume_event_payload"] = dict(resume_event_payload)
                session.parked_state = state

                lease.runnable = True
                lease.next_attempt_at = datetime.now(timezone.utc)
                for w in self._workers.values():
                    w.ready_queue.put_nowait(sid)
                    self._notify_received_total += 1
                flipped += 1
        return flipped

    # ---- Chat-turn claiming ---------------------------------------------

    async def claim_chats(
        self,
        worker_id: str,
        *,
        max_count: int,
        heartbeat_stale_after: timedelta = _DEFAULT_HEARTBEAT_STALE_AFTER,
    ) -> list[ChatLease]:
        """Iterate all Chat rows and claim eligible ones.

        Eligibility predicate (mirrors the spec):
        * status='active'
        * parked_status IS DISTINCT FROM 'parked'
        * claimed_by IS NULL OR last_heartbeat_at < now() - heartbeat_stale_after
        * turn_status='claimable' OR parked_status='resumable'
        """
        async with self._lock:
            if self._storage is None:
                return []
            chat_storage = self._storage.get_storage(Chat)
            # Collect all Chat rows from storage. Use paginated reads to
            # stay within the OffsetPage.length constraint (max 200 per page).
            page_size = 200
            all_chats: list[Chat] = []
            offset = 0
            while True:
                resp = await chat_storage.list(OffsetPage(offset=offset, length=page_size))
                all_chats.extend(resp.items)
                if len(resp.items) < page_size:
                    break
                offset += page_size

            now = datetime.now(timezone.utc)
            staleness_cutoff = now - heartbeat_stale_after

            claimed: list[ChatLease] = []
            for chat in all_chats:
                if len(claimed) >= max_count:
                    break
                # status must be 'active'
                if chat.status != "active":
                    continue
                # parked_status must NOT be 'parked'
                if chat.parked_status == "parked":
                    continue
                # claim must be available (not held, or stale)
                if chat.claimed_by is not None:
                    if chat.last_heartbeat_at is None or chat.last_heartbeat_at >= staleness_cutoff:
                        continue
                # must have work to do
                if not (
                    chat.turn_status == "claimable"
                    or chat.parked_status == "resumable"
                ):
                    continue

                # Claim it
                now_claim = datetime.now(timezone.utc)
                updated = chat.model_copy(update={
                    "turn_status": "running",
                    "claimed_by": worker_id,
                    "claimed_at": now_claim,
                    "last_heartbeat_at": now_claim,
                })
                await chat_storage.update(updated)
                claimed.append(ChatLease(
                    chat_id=chat.id,
                    worker_id=worker_id,
                    claimed_at=now_claim,
                ))
            return claimed

    async def heartbeat_chat(self, chat_id: str, worker_id: str) -> bool:
        """Bump last_heartbeat_at if we still own the claim."""
        async with self._lock:
            if self._storage is None:
                return False
            chat_storage = self._storage.get_storage(Chat)
            chat = await chat_storage.get(chat_id)
            if chat is None or chat.claimed_by != worker_id:
                return False
            updated = chat.model_copy(update={
                "last_heartbeat_at": datetime.now(timezone.utc),
            })
            await chat_storage.update(updated)
            return True

    async def release_chat(
        self,
        chat_id: str,
        worker_id: str,
        *,
        next_turn_status: Literal["idle", "claimable"],
    ) -> None:
        """Release the chat claim. No-ops if we don't own it."""
        async with self._lock:
            if self._storage is None:
                return
            chat_storage = self._storage.get_storage(Chat)
            chat = await chat_storage.get(chat_id)
            if chat is None or chat.claimed_by != worker_id:
                return
            updated = chat.model_copy(update={
                "turn_status": next_turn_status,
                "claimed_by": None,
                "claimed_at": None,
                "last_heartbeat_at": None,
            })
            await chat_storage.update(updated)

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
        concurrent ``enqueue``/``complete_turn`` may add or remove a
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
            "matrix_sessions_active": sessions_by_status,
            "matrix_sessions_runnable_queue_depth": runnable,
            "matrix_lease_expirations_total": self._lease_expirations_total,
            "matrix_scheduler_notify_received_total": (
                self._notify_received_total
            ),
        }
