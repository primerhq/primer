from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from collections.abc import AsyncIterator, Callable, Sequence
from primer.int.claim import (
    CLAIM_PRIORITY_FRESH, CLAIM_PRIORITY_RESUME, ClaimAdapter, ClaimEngine, ClaimKind, EligibilityReadError, Lease,
    ReleaseOutcome,
)
from primer.observability import tracing as _tracing
from primer.model.work_priority import validate_work_priority
import primer.observability.metrics as _metrics

logger = logging.getLogger(__name__)


@dataclass
class _LeaseRow:
    kind: ClaimKind
    entity_id: str
    # REQUIRED, with no wall-clock default: the engine's ``clock`` is what every lease timestamp is read from (a test
    # on a virtual-time loop injects its own), so a row built without the engine's time would silently mix clocks.
    next_attempt_at: datetime
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    expires_at: datetime | None = None
    priority_score: int = CLAIM_PRIORITY_FRESH
    work_priority: int = 0
    attempt_count: int = 0
    last_error: str | None = None
    # Bumped by every write to the row (a claim, a heartbeat, a release, an upsert, a mark_resumable). ``claim_due`` reads it
    # before the awaited entity read and claims only if it is unchanged after: a decision made on a row that moved meanwhile
    # (an expired holder released it, so the entity it read may have changed) is not acted on.
    generation: int = 0
    memory_occurrence: str | None = None
    memory_claim_id: str | None = None
    settling: bool = False


@dataclass
class _PriorityReads:
    # Exists only while this key has suspended readers: no permanent tombstones.
    generation: int = 0
    readers: int = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """A naive time is read as UTC, as Postgres's ``::timestamptz`` reads it under a UTC session (ticket 01a16e79).

    One lease armed with a naive ``next_attempt_at`` used to make every ``claim_due`` raise (naive against aware), so a
    single row stopped every claim of every kind.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class InMemoryClaimEngine(ClaimEngine):
    """A single-process ``ClaimEngine`` over a dict of lease rows (no row locks, no transactions).

    ``claim_due`` applies the same rule as the Postgres claim query: a lease is claimed only when it is due AND its kind's
    adapter says the entity is eligible (``ClaimAdapter.is_eligible``, the in-process twin of ``eligibility_sql``).

    ``clock`` is what every lease timestamp is read from (claim, heartbeat and expiry stamps, the expiry
    checks, ``next_attempt_at``); it defaults to the wall clock, ``datetime.now(UTC)``. A test that drives
    the pool on a virtual-time event loop passes a clock derived from that loop's time, so a lease's expiry
    is decided on the same timeline as the pool's own timers.
    """

    def __init__(
        self,
        *,
        adapters: dict[ClaimKind, ClaimAdapter],
        lease_ttl_seconds: int = 60,
        clock: Callable[[], datetime] = _utc_now,
        work_priority_enabled: bool = False,
    ) -> None:
        self._adapters = adapters
        self._work_priority_enabled = work_priority_enabled
        self._clock = clock
        self._leases: dict[tuple[ClaimKind, str], _LeaseRow] = {}
        self._priority_reads: dict[tuple[ClaimKind, str], _PriorityReads] = {}
        # The rows whose eligibility read failed, with the error types already logged for each: a bad row is logged once per
        # error type, not once per poll, until a read of it succeeds again.
        self._read_errors_logged: dict[tuple[ClaimKind, str], set[type[BaseException]]] = {}
        self._wake = asyncio.Event()
        self._notify_queue: asyncio.Queue[tuple[ClaimKind, str]] = asyncio.Queue()
        # How long a claimed lease is valid before it is considered expired
        # and reclaimable. Defaults to 60s for a standalone engine; the worker
        # pool overrides it from WorkerConfig.lease_ttl_seconds at start() so
        # the lease_ttl >= 2*heartbeat validator actually governs the engine
        # (arch review A-I1).
        self.lease_ttl_seconds = lease_ttl_seconds

    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = CLAIM_PRIORITY_FRESH,
        next_attempt_at: datetime | None = None,
    ) -> None:
        key = (kind, entity_id)
        work_priority = await self._priority_snapshot(kind, entity_id)
        existing = self._leases.get(key)
        if existing is not None:
            existing.priority_score = priority
            existing.work_priority = work_priority
            if next_attempt_at is not None:
                existing.next_attempt_at = _as_utc(next_attempt_at)
            existing.generation += 1
        else:
            self._leases[key] = _LeaseRow(
                kind=kind, entity_id=entity_id, priority_score=priority, work_priority=work_priority,
                next_attempt_at=_as_utc(next_attempt_at) if next_attempt_at is not None else self._clock(),
            )
        self._priority_changed(key)
        # Every upsert, new row or re-arm, tells the watcher (Postgres: pg_notify on every call), so a re-armed lease is not left to the next poll (CV-6).
        self._notify_queue.put_nowait((kind, entity_id))
        self._wake.set()

    async def _priority_snapshot(self, kind: ClaimKind, entity_id: str) -> int:
        if not self._work_priority_enabled:
            return 0
        adapter = self._adapters[kind]
        key = (kind, entity_id)
        reads = self._priority_reads.setdefault(key, _PriorityReads())
        reads.readers += 1
        try:
            while True:
                before = self._leases.get(key)
                generation = before.generation if before is not None else None
                mutation = reads.generation
                value = validate_work_priority(await adapter.work_priority(entity_id))
                # Keep the per-key epoch alive across absence -> row -> absence.
                # Row identity alone cannot detect that ABA; row generation still
                # fences claims/heartbeats/requeues of a surviving row.
                if (reads.generation == mutation and self._leases.get(key) is before
                        and (before is None or before.generation == generation)):
                    return value
        finally:
            reads.readers -= 1
            if not reads.readers:
                del self._priority_reads[key]

    def _priority_changed(self, key: tuple[ClaimKind, str]) -> None:
        reads = self._priority_reads.get(key)
        if reads is not None:
            reads.generation += 1

    @asynccontextmanager
    async def hold_memory_claim(self, lease: Lease, *, conn):
        row = self._leases.get((ClaimKind.MEMORY, lease.entity_id))
        if (row is None or row.claimed_by != lease.claimed_by or not lease.memory_claim_id
                or row.memory_claim_id != lease.memory_claim_id or row.memory_occurrence != lease.memory_occurrence):
            yield False
            return
        # Skip this lease in claim_due through the entire storage transaction.
        row.settling = True
        try:
            yield True
        finally:
            row.settling = False

    async def arm_memory_job(self, job, *, conn) -> None:
        work_priority = validate_work_priority(job.priority) if self._work_priority_enabled else 0
        key = (ClaimKind.MEMORY, job.id)
        row = self._leases.get(key)
        if row is not None and row.memory_occurrence == job.occurrence_id:
            return
        if row is None:
            row = _LeaseRow(*key, next_attempt_at=_as_utc(job.requested_due_at))
            self._leases[key] = row
        row.work_priority = work_priority
        row.memory_occurrence = job.occurrence_id
        row.next_attempt_at = _as_utc(job.requested_due_at)
        row.attempt_count = job.retry_count
        row.last_error = job.retry_error
        row.generation += 1
        self._priority_changed(key)
        self._notify_queue.put_nowait(key)
        self._wake.set()

    async def discard_memory_lease(self, entity_id: str, occurrence: str | None) -> None:
        key = (ClaimKind.MEMORY, entity_id)
        row = self._leases.get(key)
        if row is not None and row.memory_occurrence == occurrence:
            await self.delete_lease(*key)

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        key = (kind, entity_id)
        self._leases.pop(key, None)
        self._priority_changed(key)
        self._read_errors_logged.pop(key, None)

    async def has_lease(self, kind: ClaimKind, entity_id: str, *, work_priority: int | None = None) -> bool:
        row = self._leases.get((kind, entity_id))
        return row is not None and (
            work_priority is None or not self._work_priority_enabled or row.work_priority == work_priority
        )

    async def lease_exists(self, kind: ClaimKind, entity_ids: Sequence[str]) -> set[str]:
        return {eid for eid in entity_ids if (kind, eid) in self._leases}

    async def prune_dead_leases(self, kind: ClaimKind) -> int:
        adapter = self._adapters.get(kind)
        if adapter is None or adapter.dead_lease_sql() is None:
            return 0
        def held(row: _LeaseRow) -> bool:
            return row.claimed_by is not None and (row.expires_at is None or row.expires_at >= self._clock())

        pruned = 0
        for key, row in list(self._leases.items()):
            if row.kind != kind or held(row):
                continue                                  # held: its holder's release owns it
            # ``is_dead`` awaits, and a claim can land meanwhile (claim_due mutates the row in place): decide
            # again after it, with no await between the last check and the delete.
            occurrence = row.memory_occurrence
            if (await adapter.is_dead(row.entity_id) and self._leases.get(key) is row
                    and row.memory_occurrence == occurrence and not held(row)):
                del self._leases[key]
                self._priority_changed(key)
                pruned += 1
        return pruned

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        row = self._leases.get((kind, entity_id))
        if row is None or row.claimed_by is None or row.expires_at is None:
            return False
        return row.expires_at > self._clock()

    @staticmethod
    def _due(row: _LeaseRow, now: datetime) -> bool:
        """The lease half of the claim query: unclaimed or expired, and ``next_attempt_at`` reached (a naive one read as UTC)."""
        return (
            not row.settling and (row.claimed_by is None or (row.expires_at is not None and row.expires_at < now))
            and _as_utc(row.next_attempt_at) <= now
        )

    async def _eligible(self, row: _LeaseRow, now: datetime) -> bool:
        """The entity half of the claim query: the kind's :meth:`~primer.int.claim.ClaimAdapter.is_eligible`.

        A kind with no adapter stays claimable (this engine never required one). A failed read or decode of the entity
        (:class:`~primer.int.claim.EligibilityReadError`) skips the lease for this claim only, so one unreadable row cannot
        stop every other claim; the next poll asks again. It is counted every time
        (``claim_eligibility_read_errors_total{kind}``) and logged once per row and error type, with its traceback, until a
        read of that row succeeds. Anything else the adapter raises (an ``AttributeError`` or ``TypeError`` is a bug in the
        adapter, not an unreadable row) propagates out of ``claim_due``.
        """
        adapter = self._adapters.get(row.kind)
        if adapter is None:
            return True
        key = (row.kind, row.entity_id)
        try:
            eligible = await adapter.is_eligible(row.entity_id, now)
            if eligible and self._work_priority_enabled:
                eligible = await adapter.priority_snapshot_matches(row.entity_id, row.work_priority)
        except EligibilityReadError as exc:
            _metrics.claim_eligibility_read_errors_total.labels(row.kind.value).inc()
            cause = type(exc.__cause__ or exc)
            logged = self._read_errors_logged.setdefault(key, set())
            if cause not in logged:
                logged.add(cause)
                logger.warning(
                    "claim_due: could not read %s/%s to decide its eligibility; it is not claimed until a read succeeds "
                    "(logged once per error type): %s",
                    row.kind.value, row.entity_id, exc, exc_info=exc,
                )
            return False
        self._read_errors_logged.pop(key, None)
        return eligible

    async def claim_due(
        self, worker_id: str, *, max_count: int, kinds: list[ClaimKind] | None = None,
        max_work_priority: int | None = None,
    ) -> list[Lease]:
        """Claim up to ``max_count`` leases that are due AND whose kind says their entity is eligible.

        The same rule as the Postgres claim query (``primer.claim.sql``): a lease row must be unclaimed or expired with
        ``next_attempt_at`` reached, and its entity must pass the kind's eligibility (here
        :meth:`~primer.int.claim.ClaimAdapter.is_eligible`, the twin of the ``eligibility_sql`` the query joins in), so a
        released lease whose entity is not due (a parked session, a finished harness operation, a trigger before its tick,
        a finished tool call) is not claimed again. Eligibility is decided before ``max_count`` is applied, in
        ``(priority_score, next_attempt_at)`` order (prefixed by work priority when enabled), as the query's per-kind
        filter runs before its ``LIMIT``.

        Reading an entity awaits, and another claim or a release can run meanwhile, so each row is checked again after its
        read, with no await between that check and the stamp: it must still be this engine's row, unwritten since candidate
        selection (its ``generation`` also fences changes while an earlier candidate's read awaited), and
        still due. The decision is made at ``now``, the claim's start; the stamps (``claimed_at``, ``expires_at``) are read
        from the clock when they are written.
        """
        if max_work_priority is not None:
            validate_work_priority(max_work_priority)
            if not self._work_priority_enabled:
                raise ValueError("work priority is not enabled")
        _tracer = _tracing.get_tracer("primer.claim")
        with _tracer.start_as_current_span("claim.due") as _span:
            now = self._clock()
            kind_set = None if kinds is None else set(kinds)
            candidates = [
                (row, row.generation) for row in self._leases.values()
                if self._due(row, now) and (kind_set is None or row.kind in kind_set)
                and (max_work_priority is None or row.work_priority <= max_work_priority)
            ]
            candidates.sort(key=lambda candidate: (
                candidate[0].work_priority if self._work_priority_enabled else 0,
                candidate[0].priority_score, _as_utc(candidate[0].next_attempt_at),
            ))
            out: list[Lease] = []
            for row, generation in candidates:
                if len(out) >= max_count:
                    break
                if self._leases.get((row.kind, row.entity_id)) is not row or row.generation != generation:
                    continue  # An earlier candidate awaited while this one's admission/order changed.
                if not await self._eligible(row, now):
                    continue
                if (
                    self._leases.get((row.kind, row.entity_id)) is not row
                    or row.generation != generation
                    or not self._due(row, now)
                    or (max_work_priority is not None and row.work_priority > max_work_priority)
                ):
                    continue                              # dropped, claimed, released or re-armed while its entity was read
                stamp = self._clock()
                wait = max(0.0, (stamp - _as_utc(row.next_attempt_at)).total_seconds())
                if row.kind == ClaimKind.MEMORY:
                    row.memory_claim_id = uuid4().hex
                row.claimed_by = worker_id
                row.claimed_at = stamp
                row.last_heartbeat_at = stamp
                row.expires_at = stamp + timedelta(seconds=self.lease_ttl_seconds)
                row.generation += 1
                lease = Lease(
                    kind=row.kind, entity_id=row.entity_id, claimed_by=worker_id,
                    claimed_at=stamp, expires_at=row.expires_at,
                    attempt_count=row.attempt_count, last_error=row.last_error,
                    memory_occurrence=row.memory_occurrence, memory_claim_id=row.memory_claim_id,
                    work_priority=row.work_priority if self._work_priority_enabled else 0,
                )
                out.append(lease)
                _metrics.claim_enqueue_latency_seconds.labels(
                    lease.kind.value
                ).observe(wait)
                if self._work_priority_enabled:
                    _metrics.claim_work_priority_total.labels(
                        lease.kind.value, "foreground" if lease.work_priority == 0 else "background",
                    ).inc()
                _span.add_event("claim_assigned", {"kind": lease.kind.value})
            # The claim-time hooks run before the leases are handed out: an
            # adapter that owns a claim-scoped entity field (the session
            # adapter's last_worker_id stamp) writes it while the claim is
            # fresh, so the claiming worker observes a stamped row, and a
            # failing hook fails the claim instead of a half-stamped hand-out.
            for lease in out:
                adapter = self._adapters.get(lease.kind)
                if adapter is not None:
                    await adapter.on_claim(None, lease)
            _span.set_attribute("claim.count", len(out))
            return out

    async def heartbeat(
        self, worker_id: str, kind_ids: list[tuple[ClaimKind, str]],
        *, memory_claims: dict[str, str] | None = None,
    ) -> list[tuple[ClaimKind, str]]:
        now = self._clock()
        confirmed = []
        for kind, entity_id in kind_ids:
            row = self._leases.get((kind, entity_id))
            if kind == ClaimKind.MEMORY and (row is None or not row.memory_claim_id
                                              or row.memory_claim_id != (memory_claims or {}).get(entity_id)):
                continue
            if row is not None and row.claimed_by == worker_id:
                row.last_heartbeat_at = now
                row.expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
                row.generation += 1
                confirmed.append((kind, entity_id))
        return confirmed

    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None:
        key = (lease.kind, lease.entity_id)
        row = self._leases.get(key)
        # Fence: only mutate the lease + run on_release while THIS worker
        # still owns the lease. If the row is gone or was re-claimed by
        # another worker, skip entirely so a stale/dead worker cannot
        # clobber the new owner's entity state.
        if row is None or row.claimed_by != lease.claimed_by:
            logger.warning(
                "claim release skipped: lease %s/%s no longer owned by %r "
                "(presumed re-claimed)",
                lease.kind.value, lease.entity_id, lease.claimed_by,
            )
            return
        # Run the adapter's on_release hook (which clears the entity's park
        # columns) BEFORE releasing the claim, then reset the lease's claim
        # fields. Postgres does both inside a single transaction so a
        # concurrent claim never observes a freed lease whose entity row is
        # still 'resumable'. The in-memory engine has no transaction, so we
        # preserve the same invariant by ordering: while ``claimed_by`` is
        # still set, ``claim_due`` cannot re-claim this lease, so the
        # on_release entity-state write (e.g. parked_status -> None) is
        # guaranteed to land before the lease becomes claimable again. The
        # previous order (reset claim fields first, then await on_release)
        # opened a window where a resumable session was re-claimed and its
        # resume hook ran twice, double-executing an approved tool.
        adapter = self._adapters.get(lease.kind)
        wake_signal = None
        if adapter is not None and not outcome.entity_noop:
            wake_signal = await adapter.on_release(
                conn=None, entity_id=lease.entity_id, outcome=outcome,
            )
        if lease.kind == ClaimKind.MEMORY:
            # on_release can suspend. Recheck the current row and holder
            # before applying the old disposition to this stable key.
            if (self._leases.get(key) is not row or row.claimed_by != lease.claimed_by
                    or not lease.memory_claim_id or row.memory_claim_id != lease.memory_claim_id):
                return
            if row.memory_occurrence != lease.memory_occurrence:
                outcome = ReleaseOutcome(success=True, entity_noop=True)
        if outcome.drop_lease:
            self._leases.pop(key, None)
            self._priority_changed(key)
            self._read_errors_logged.pop(key, None)
        else:
            row.claimed_by = None
            row.claimed_at = None
            row.last_heartbeat_at = None
            row.expires_at = None
            row.generation += 1
            # As the SQL: a release puts the lease back in line at now + the delay, except a lease-only hand-back with no explicit delay, which
            # keeps its place (CV-7).
            if outcome.requeue_after is not None:
                row.next_attempt_at = self._clock() + outcome.requeue_after
            elif not outcome.entity_noop:
                row.next_attempt_at = self._clock()
            if outcome.entity_noop:
                pass  # a lease-only hand-back is not a run: attempt_count and last_error stay
            elif not outcome.success:
                row.attempt_count += 1
                row.last_error = outcome.last_error
            else:
                row.attempt_count = 0
                row.last_error = None
            self._wake.set()
        # 01a0518b review: no real transaction here (everything above is
        # synchronous in-process), so "post-commit" degrades to "post
        # mutation" - call the hook only once this lease's own state is
        # fully settled, mirroring PostgresClaimEngine's own ordering
        # (outside its conn.transaction() block).
        if wake_signal is not None and self._post_release_hook is not None:
            await self._post_release_hook(wake_signal)

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = CLAIM_PRIORITY_RESUME,
    ) -> None:
        await self.upsert(kind, entity_id, priority=priority, next_attempt_at=self._clock())

    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:
        while True:
            item = await self._notify_queue.get()
            yield item
