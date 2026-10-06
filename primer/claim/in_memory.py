from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from collections.abc import AsyncIterator, Callable, Sequence
from primer.int.claim import (
    CLAIM_PRIORITY_FRESH, CLAIM_PRIORITY_RESUME, ClaimAdapter, ClaimEngine, ClaimKind, Lease,
    ReleaseOutcome,
)
from primer.observability import tracing as _tracing
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
    attempt_count: int = 0
    last_error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryClaimEngine(ClaimEngine):
    """A single-process ``ClaimEngine`` over a dict of lease rows (no row locks, no transactions).

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
    ) -> None:
        self._adapters = adapters
        self._clock = clock
        self._leases: dict[tuple[ClaimKind, str], _LeaseRow] = {}
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
        existing = self._leases.get(key)
        if existing is not None:
            existing.priority_score = priority
            if next_attempt_at is not None:
                existing.next_attempt_at = next_attempt_at
        else:
            self._leases[key] = _LeaseRow(
                kind=kind, entity_id=entity_id, priority_score=priority,
                next_attempt_at=next_attempt_at or self._clock(),
            )
            self._notify_queue.put_nowait((kind, entity_id))
        self._wake.set()

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        self._leases.pop((kind, entity_id), None)

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        return (kind, entity_id) in self._leases

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
            if await adapter.is_dead(row.entity_id) and self._leases.get(key) is row and not held(row):
                del self._leases[key]
                pruned += 1
        return pruned

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        row = self._leases.get((kind, entity_id))
        if row is None or row.claimed_by is None or row.expires_at is None:
            return False
        return row.expires_at > self._clock()

    async def claim_due(
        self, worker_id: str, *, max_count: int, kinds: list[ClaimKind] | None = None,
    ) -> list[Lease]:
        _tracer = _tracing.get_tracer("primer.claim")
        with _tracer.start_as_current_span("claim.due") as _span:
            now = self._clock()
            kind_set = None if kinds is None else set(kinds)
            eligible = [
                row for row in self._leases.values()
                if (row.claimed_by is None
                    or (row.expires_at is not None and row.expires_at < now))
                and row.next_attempt_at <= now
                and (kind_set is None or row.kind in kind_set)
            ]
            eligible.sort(key=lambda r: (r.priority_score, r.next_attempt_at))
            chosen = eligible[:max_count]
            out: list[Lease] = []
            for row in chosen:
                wait = max(0.0, (now - row.next_attempt_at).total_seconds())
                row.claimed_by = worker_id
                row.claimed_at = now
                row.last_heartbeat_at = now
                row.expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
                lease = Lease(
                    kind=row.kind, entity_id=row.entity_id, claimed_by=worker_id,
                    claimed_at=now, expires_at=row.expires_at,
                    attempt_count=row.attempt_count, last_error=row.last_error,
                )
                out.append(lease)
                _metrics.claim_enqueue_latency_seconds.labels(
                    lease.kind.value
                ).observe(wait)
                _span.add_event("claim_assigned", {"kind": lease.kind.value})
            _span.set_attribute("claim.count", len(out))
            return out

    async def heartbeat(
        self, worker_id: str, kind_ids: list[tuple[ClaimKind, str]],
    ) -> list[tuple[ClaimKind, str]]:
        now = self._clock()
        confirmed = []
        for kind, entity_id in kind_ids:
            row = self._leases.get((kind, entity_id))
            if row is not None and row.claimed_by == worker_id:
                row.last_heartbeat_at = now
                row.expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
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
        if outcome.drop_lease:
            self._leases.pop(key, None)
        else:
            row.claimed_by = None
            row.claimed_at = None
            row.last_heartbeat_at = None
            row.expires_at = None
            if outcome.requeue_after is not None:
                row.next_attempt_at = self._clock() + outcome.requeue_after
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
        row = self._leases.get((kind, entity_id))
        if row is None:
            await self.upsert(kind, entity_id, priority=priority)
            return
        row.priority_score = priority
        row.next_attempt_at = self._clock()
        self._wake.set()
        self._notify_queue.put_nowait((kind, entity_id))

    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:
        while True:
            item = await self._notify_queue.get()
            yield item
