from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from collections.abc import AsyncIterator
from primer.int.claim import (
    ClaimAdapter, ClaimEngine, ClaimKind, Lease, ReleaseOutcome,
)
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


@dataclass
class _LeaseRow:
    kind: ClaimKind
    entity_id: str
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    expires_at: datetime | None = None
    next_attempt_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    priority_score: int = 100
    attempt_count: int = 0
    last_error: str | None = None


class InMemoryClaimEngine(ClaimEngine):
    def __init__(self, *, adapters: dict[ClaimKind, ClaimAdapter]) -> None:
        self._adapters = adapters
        self._leases: dict[tuple[ClaimKind, str], _LeaseRow] = {}
        self._wake = asyncio.Event()
        self._notify_queue: asyncio.Queue[tuple[ClaimKind, str]] = asyncio.Queue()

    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100,
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
                next_attempt_at=next_attempt_at or datetime.now(UTC),
            )
            self._notify_queue.put_nowait((kind, entity_id))
        self._wake.set()

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        self._leases.pop((kind, entity_id), None)

    LEASE_TTL = timedelta(seconds=60)

    async def claim_due(self, worker_id: str, *, max_count: int) -> list[Lease]:
        _tracer = _tracing.get_tracer("primer.claim")
        with _tracer.start_as_current_span("claim.due") as _span:
            now = datetime.now(UTC)
            eligible = [
                row for row in self._leases.values()
                if (row.claimed_by is None
                    or (row.expires_at is not None and row.expires_at < now))
                and row.next_attempt_at <= now
            ]
            eligible.sort(key=lambda r: (r.priority_score, r.next_attempt_at))
            chosen = eligible[:max_count]
            out: list[Lease] = []
            for row in chosen:
                wait = max(0.0, (now - row.next_attempt_at).total_seconds())
                row.claimed_by = worker_id
                row.claimed_at = now
                row.last_heartbeat_at = now
                row.expires_at = now + self.LEASE_TTL
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
        now = datetime.now(UTC)
        confirmed = []
        for kind, entity_id in kind_ids:
            row = self._leases.get((kind, entity_id))
            if row is not None and row.claimed_by == worker_id:
                row.last_heartbeat_at = now
                row.expires_at = now + self.LEASE_TTL
                confirmed.append((kind, entity_id))
        return confirmed

    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None:
        key = (lease.kind, lease.entity_id)
        if outcome.drop_lease:
            self._leases.pop(key, None)
            # Run adapter on_release before returning
            adapter = self._adapters.get(lease.kind)
            if adapter is not None:
                await adapter.on_release(conn=None, entity_id=lease.entity_id, outcome=outcome)
            return
        row = self._leases.get(key)
        if row is None:
            return
        row.claimed_by = None
        row.claimed_at = None
        row.last_heartbeat_at = None
        row.expires_at = None
        if outcome.requeue_after is not None:
            row.next_attempt_at = datetime.now(UTC) + outcome.requeue_after
        if not outcome.success:
            row.attempt_count += 1
            row.last_error = outcome.last_error
        else:
            row.attempt_count = 0
            row.last_error = None
        # Run adapter on_release hook
        adapter = self._adapters.get(lease.kind)
        if adapter is not None:
            await adapter.on_release(conn=None, entity_id=lease.entity_id, outcome=outcome)
        self._wake.set()

    async def mark_resumable(self, kind: ClaimKind, entity_id: str, *, priority: int = 50) -> None:
        row = self._leases.get((kind, entity_id))
        if row is None:
            await self.upsert(kind, entity_id, priority=priority)
            return
        row.priority_score = priority
        row.next_attempt_at = datetime.now(UTC)
        self._wake.set()
        self._notify_queue.put_nowait((kind, entity_id))

    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]:
        while True:
            item = await self._notify_queue.get()
            yield item
