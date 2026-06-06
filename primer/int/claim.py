from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class ClaimKind(StrEnum):
    SESSION = "session"
    CHAT = "chat"
    HARNESS = "harness"
    TRIGGER = "trigger"


@dataclass(frozen=True)
class Lease:
    kind: ClaimKind
    entity_id: str
    claimed_by: str
    claimed_at: datetime
    expires_at: datetime
    attempt_count: int
    last_error: str | None


@dataclass(frozen=True)
class ParkRequest:
    """Request to park an entity instead of completing/failing its turn.

    Set on :class:`ReleaseOutcome` when a session turn hits a yielding
    tool. The claim adapter's ``on_release`` writes these into the
    entity row's park columns (parked_status='parked') and the engine
    drops the lease, so the parked entity is not re-claimed until the
    resume event re-arms it.
    """

    parked_state: dict[str, Any]
    parked_event_key: str
    parked_until: datetime | None
    parked_at: datetime


@dataclass(frozen=True)
class ReleaseOutcome:
    success: bool
    requeue_after: timedelta | None = None
    last_error: str | None = None
    drop_lease: bool = False
    park: ParkRequest | None = None


class ClaimAdapter(ABC):
    kind: ClaimKind
    entity_table: str

    @abstractmethod
    def eligibility_sql(self) -> str: ...

    @abstractmethod
    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None: ...


class ClaimEngine(ABC):
    @abstractmethod
    async def claim_due(self, worker_id: str, *, max_count: int) -> list[Lease]: ...

    @abstractmethod
    async def heartbeat(
        self, worker_id: str, kind_ids: list[tuple[ClaimKind, str]],
    ) -> list[tuple[ClaimKind, str]]: ...

    @abstractmethod
    async def release(self, lease: Lease, *, outcome: ReleaseOutcome) -> None: ...

    @abstractmethod
    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 50,
    ) -> None: ...

    @abstractmethod
    async def watch_ready(self) -> AsyncIterator[tuple[ClaimKind, str]]: ...

    @abstractmethod
    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100,
        next_attempt_at: datetime | None = None,
    ) -> None: ...

    @abstractmethod
    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None: ...
