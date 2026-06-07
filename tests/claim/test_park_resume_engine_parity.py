"""Cross-engine guard tests for F10c park/resume invariants.

These tests PIN the exact failure modes the prior approach missed:

- No-lease-while-parked: after a park release on the IN-MEMORY engine,
  claim_due returns nothing for that session.  The no-loop guarantee comes
  from the dropped lease, NOT from eligibility_sql which the in-memory
  engine ignores.

- Re-arm bridge: after the listener-style flip + engine.mark_resumable,
  claim_due returns the session.

- mark_resumable idempotency: a second re-arm yields exactly one claimable
  lease, not duplicates.

Postgres-engine parity for park/resume is covered by the distributed lane
(SMK-DST-06, Task 8) and is intentionally omitted here.  The in-memory
engine is the required core for this unit lane.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SID = "sess-park-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_session(session_id: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_no=1,
    )


class _FakeStorage:
    """Minimal in-memory storage stub matching the Storage protocol."""

    def __init__(self, session: WorkspaceSession) -> None:
        self._session = session

    async def get(self, id: str) -> WorkspaceSession | None:
        return self._session if self._session.id == id else None

    async def update(self, entity: WorkspaceSession) -> WorkspaceSession:
        self._session = entity
        return entity


def _make_engine(storage: _FakeStorage) -> InMemoryClaimEngine:
    adapter = SessionClaimAdapter(session_storage=storage)
    return InMemoryClaimEngine(adapters={ClaimKind.SESSION: adapter})


def _make_park_outcome() -> ReleaseOutcome:
    now = _now()
    park = ParkRequest(
        parked_state={"schema_version": 1, "yielded": {"tool_name": "ask_user"}},
        parked_event_key=f"ask_user:{_SID}:tc-1",
        parked_until=now,
        parked_at=now,
    )
    return ReleaseOutcome(success=True, drop_lease=True, park=park)


# ---------------------------------------------------------------------------
# Test 1: No-lease-while-parked
# ---------------------------------------------------------------------------


async def test_no_lease_while_parked_inmemory() -> None:
    """After a park release, claim_due must return nothing for that session.

    This pins the core F10c invariant on the in-memory engine: the no-loop
    guarantee comes from the dropped lease (drop_lease=True), not from
    eligibility_sql (which the in-memory engine does not evaluate).
    """
    storage = _FakeStorage(_make_session(_SID))
    engine = _make_engine(storage)

    # Seed a claimable lease for the session.
    await engine.upsert(ClaimKind.SESSION, _SID)

    # Claim it.
    leases = await engine.claim_due("wrk-1", max_count=5)
    assert len(leases) == 1
    assert leases[0].entity_id == _SID

    # Release with a park outcome (drop_lease=True, park=ParkRequest).
    await engine.release(leases[0], outcome=_make_park_outcome())

    # After park: the lease must be gone -- claim_due returns nothing.
    after_park = await engine.claim_due("wrk-2", max_count=5)
    entity_ids = [l.entity_id for l in after_park]
    assert _SID not in entity_ids, (
        f"F10c regression: parked session {_SID!r} is still claimable "
        f"(returned leases: {entity_ids})"
    )

    # The storage row must reflect the parked state.
    row = await storage.get(_SID)
    assert row is not None
    assert row.parked_status == "parked", (
        f"Expected parked_status='parked', got {row.parked_status!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Re-arm bridge makes resumable claimable
# ---------------------------------------------------------------------------


async def test_rearm_makes_resumable_claimable_inmemory() -> None:
    """After a park, simulating the listener flip + mark_resumable re-arms the
    session so claim_due returns it again.

    This pins the full park-resume round-trip on the in-memory engine.
    """
    storage = _FakeStorage(_make_session(_SID))
    engine = _make_engine(storage)

    # Seed and claim.
    await engine.upsert(ClaimKind.SESSION, _SID)
    [lease] = await engine.claim_due("wrk-1", max_count=5)

    # Park it.
    await engine.release(lease, outcome=_make_park_outcome())

    # Sanity: nothing claimable right after the park.
    assert _SID not in [l.entity_id for l in await engine.claim_due("wrk-2", max_count=5)]

    # Simulate the resume-event listener:
    #   1. Load the row and flip parked_status -> "resumable".
    #   2. Optionally write resume_event_payload into parked_state.
    row = await storage.get(_SID)
    assert row is not None
    updated = row.model_copy(update={
        "parked_status": "resumable",
        "parked_state": {
            **(row.parked_state or {}),
            "resume_event_payload": {"event": "user_replied", "msg_id": "m1"},
        },
    })
    await storage.update(updated)

    #   3. Re-arm the engine (mark_resumable re-creates the lease).
    await engine.mark_resumable(ClaimKind.SESSION, _SID)

    # Now claim_due must return the session.
    resumed = await engine.claim_due("wrk-3", max_count=5)
    entity_ids = [l.entity_id for l in resumed]
    assert _SID in entity_ids, (
        f"F10c regression: re-armed session {_SID!r} not returned by claim_due "
        f"(returned leases: {entity_ids})"
    )


# ---------------------------------------------------------------------------
# Test 3: mark_resumable idempotency
# ---------------------------------------------------------------------------


async def test_mark_resumable_idempotent_inmemory() -> None:
    """Calling mark_resumable twice must produce exactly one claimable lease,
    not duplicates.

    Idempotency guarantee: a double re-arm should not enqueue the session
    twice or allow it to be claimed more than once.
    """
    storage = _FakeStorage(_make_session(_SID))
    engine = _make_engine(storage)

    # Start from the parked state (no lease present) by seeding, claiming,
    # and releasing with a park outcome.
    await engine.upsert(ClaimKind.SESSION, _SID)
    [lease] = await engine.claim_due("wrk-1", max_count=5)
    await engine.release(lease, outcome=_make_park_outcome())

    # Double re-arm.
    await engine.mark_resumable(ClaimKind.SESSION, _SID)
    await engine.mark_resumable(ClaimKind.SESSION, _SID)

    # claim_due should return the session exactly once.
    leases = await engine.claim_due("wrk-2", max_count=10)
    sid_count = sum(1 for l in leases if l.entity_id == _SID)
    assert sid_count == 1, (
        f"mark_resumable idempotency violation: {_SID!r} appeared {sid_count} "
        f"time(s) in claim_due results (expected 1)"
    )
