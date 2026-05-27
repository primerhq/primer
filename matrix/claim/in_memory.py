from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from collections.abc import AsyncIterator
from matrix.int.claim import (
    ClaimAdapter, ClaimEngine, ClaimKind, Lease, ReleaseOutcome,
)


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
        self._wake.set()

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        self._leases.pop((kind, entity_id), None)

    # Remaining ABC methods raise NotImplementedError; filled in next tasks.
    async def claim_due(self, worker_id, *, max_count): raise NotImplementedError
    async def heartbeat(self, worker_id, kind_ids): raise NotImplementedError
    async def release(self, lease, *, outcome): raise NotImplementedError
    async def mark_resumable(self, kind, entity_id, *, priority=50): raise NotImplementedError
    async def watch_ready(self): raise NotImplementedError
