"""StuckSessionSweeper — ends sessions whose first turn never ran.

Nothing else reaps these: TimeoutSweeper only handles parks (a turn that started and is
waiting), and cancel just sets a flag a worker reads on its next step. A session that never
started has no worker, so it stays non-terminal forever — and a `parallelism="skip"`
subscription will not fire while any attributed session is non-terminal, so one stuck row
silently halts its trigger.

The sweeper's whole difficulty is telling "never started" from "started and still going",
because `turn_no` cannot: it is bumped on RELEASE, so a first turn running for hours still
reads 0. Only a live claim lease separates the two, which is why every test here supplies a
claim engine — a sweeper without one is deliberately inert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.scheduler_tasks import StuckSessionSweeper
from primer.int.claim import ClaimKind
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


class _FakeClaimEngine:
    """Just the one method the sweeper calls, over a set of session ids that have a lease ROW.

    ``has_live_lease`` is poisoned on purpose: the sweeper must ask whether a lease row
    exists (queued, running or awaiting reclaim), not whether a worker holds a live claim.
    """

    def __init__(self, leased: set[str] | None = None, *, raises: bool = False) -> None:
        self.leased = leased or set()
        self.raises = raises
        self.calls: list[tuple[ClaimKind, str]] = []

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        self.calls.append((kind, entity_id))
        if self.raises:
            raise RuntimeError("lease table unreachable")
        return entity_id in self.leased

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        raise AssertionError("the sweeper must not key on a LIVE claim, only on a lease row")


def _sweeper(storage, *, leased=None, raises=False, **kw):
    return StuckSessionSweeper(
        session_storage=storage,
        claim_engine=_FakeClaimEngine(leased, raises=raises),
        **kw,
    )


def _session(sid, *, age_seconds=3600, turn_no=0, status=SessionStatus.RUNNING, **over):
    base = dict(
        id=sid,
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-1"),
        status=status,
        turn_status="idle",
        turn_no=turn_no,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )
    base.update(over)
    return WorkspaceSession(**base)


@pytest.mark.asyncio
async def test_ends_a_session_whose_first_turn_never_ran(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-stuck"))

    reaped = await _sweeper(storage)._tick()

    assert reaped == 1
    row = await storage.get("se-stuck")
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "failed"
    assert row.ended_detail == "never_started"
    assert row.ended_at is not None


@pytest.mark.asyncio
async def test_leaves_a_first_turn_that_is_still_running(fake_storage_provider):
    """The regression that motivated the lease check.

    turn_no is bumped on release, so a session whose FIRST turn is mid-flight still reads
    turn_no == 0 — indistinguishable by that field alone from one that was never claimed.
    In production this reaped a daily rating job at the 10-minute mark while its worker
    went on computing for another three hours: the row claimed ENDED, and the released
    `parallelism="skip"` gate let the next tick start a second concurrent run.
    """
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-inflight", age_seconds=12_600, turn_no=0))

    sweeper = _sweeper(storage, leased={"se-inflight"})
    reaped = await sweeper._tick()

    assert reaped == 0
    row = await storage.get("se-inflight")
    assert row.status == SessionStatus.RUNNING
    assert row.ended_reason is None


@pytest.mark.asyncio
async def test_reaps_once_the_lease_row_is_gone(fake_storage_provider):
    """A lost claim leaves no lease row, so the session is reapable.

    This is the other half of the lease check: it must not turn the sweeper off for the
    abandoned sessions it exists to clean up.
    """
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-abandoned", age_seconds=12_600, turn_no=0))

    assert await _sweeper(storage, leased={"se-abandoned"})._tick() == 0
    assert await _sweeper(storage, leased=set())._tick() == 1
    assert (await storage.get("se-abandoned")).ended_detail == "never_started"


@pytest.mark.asyncio
async def test_an_unreadable_lease_never_authorises_a_reap(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-unknown"))

    assert await _sweeper(storage, raises=True)._tick() == 0
    assert (await storage.get("se-unknown")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_without_a_claim_engine_the_sweeper_is_inert(fake_storage_provider):
    """No engine means no way to prove a session is idle, so it reaps nothing."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-stuck"))

    assert await StuckSessionSweeper(session_storage=storage)._tick() == 0
    assert (await storage.get("se-stuck")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_leaves_a_running_turn_alone_however_long_it_runs(fake_storage_provider):
    """The bound is on NEVER STARTING, never on duration.

    A started turn may legitimately take hours (a graph build, a long exec). Ending it from
    under the worker would be far worse than leaving it.
    """
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        _session("se-working", age_seconds=86_400, turn_no=4, turn_status="running")
    )

    reaped = await _sweeper(storage)._tick()

    assert reaped == 0
    assert (await storage.get("se-working")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_leaves_a_freshly_created_session_alone(fake_storage_provider):
    """Inside the grace window the claim is simply still in flight."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-fresh", age_seconds=5))

    reaped = await _sweeper(storage)._tick()

    assert reaped == 0
    assert (await storage.get("se-fresh")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_ignores_already_ended_sessions(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        _session("se-done", status=SessionStatus.ENDED, ended_reason="completed")
    )

    assert await _sweeper(storage)._tick() == 0
    assert (await storage.get("se-done")).ended_reason == "completed"


@pytest.mark.asyncio
async def test_the_lease_is_checked_for_sessions_not_some_other_kind(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-stuck"))

    engine = _FakeClaimEngine()
    await StuckSessionSweeper(session_storage=storage, claim_engine=engine)._tick()

    assert engine.calls == [(ClaimKind.SESSION, "se-stuck")]


@pytest.mark.asyncio
async def test_grace_is_configurable(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-recent", age_seconds=30))

    assert await _sweeper(storage, grace_seconds=3600)._tick() == 0
    assert await _sweeper(storage, grace_seconds=10)._tick() == 1


# A park does not bump ``turn_no`` and releases the session's lease, so a first turn that
# is parked on a human (or a timer, or an event) looks exactly like a session that never
# started: turn 0, no live lease, old. The sweeper used to end it as "never_started" ten
# minutes in, while the human was still deciding.


def _parked(sid, *, parked_status="parked", **over):
    now = datetime.now(timezone.utc)
    base = dict(
        parked_status=parked_status,
        parked_event_key=f"tool_approval:{sid}:call_0",
        parked_at=now - timedelta(seconds=3000),
        parked_until=now + timedelta(seconds=600),
        parked_state={"yielded": {"tool_name": "_approval"}},
    )
    base.update(over)
    return _session(sid, turn_no=0, age_seconds=3600, **base)


@pytest.mark.asyncio
async def test_leaves_a_first_turn_that_is_parked_alone(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-parked"))

    reaped = await _sweeper(storage)._tick()

    assert reaped == 0
    row = await storage.get("se-parked")
    assert row.status == SessionStatus.RUNNING
    assert row.ended_detail is None
    assert row.parked_status == "parked"


@pytest.mark.asyncio
async def test_leaves_a_first_turn_that_is_resumable_alone(fake_storage_provider):
    """The event fired and the row is waiting for a claim; it is no more stuck than a
    parked one."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-resumable", parked_status="resumable"))

    reaped = await _sweeper(storage)._tick()

    assert reaped == 0
    assert (await storage.get("se-resumable")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_still_reaps_a_first_turn_whose_park_was_cleared(fake_storage_provider):
    """Control for the two above: only an ACTIVE park exempts a session. Park columns
    left over from a park that has since been cleared (parked_status back to None) must
    not shelter a session that never went on to run."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked(
        "se-cleared", parked_status=None, parked_event_key=None, parked_state=None,
    ))

    reaped = await _sweeper(storage)._tick()

    assert reaped == 1
    row = await storage.get("se-cleared")
    assert row.status == SessionStatus.ENDED
    assert row.ended_detail == "never_started"


# The check is for a lease ROW, not a live claim. A row means the session is running,
# queued behind a busy pool, or awaiting reclaim after its worker died; the claim loop will
# run it. These use the real in-memory engine so they cannot drift from its semantics.


def _real_sweeper(storage, engine):
    return StuckSessionSweeper(session_storage=storage, claim_engine=engine)


@pytest.mark.asyncio
async def test_leaves_a_first_turn_queued_behind_a_busy_pool_alone(fake_storage_provider):
    """An armed lease nobody has claimed yet is not a live claim, but it is not lost either.

    A fresh first turn can wait more than the grace period behind a saturated pool; ending
    it as never_started while its lease sat in the queue was a second false positive.
    """
    from primer.claim.in_memory import InMemoryClaimEngine

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-queued", age_seconds=3600, turn_no=0))
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "se-queued")
    assert await engine.has_live_lease(ClaimKind.SESSION, "se-queued") is False

    reaped = await _real_sweeper(storage, engine)._tick()

    assert reaped == 0
    assert (await storage.get("se-queued")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_leaves_a_first_turn_whose_worker_died_to_the_claim_loop(fake_storage_provider):
    """A dead worker's lease row survives until reclaimed; the next worker re-runs the turn
    (the system's ordinary at-least-once recovery) instead of the sweeper ending it."""
    from primer.claim.in_memory import InMemoryClaimEngine

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-dead-worker", age_seconds=3600, turn_no=0))
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.SESSION, "se-dead-worker")
    await engine.claim_due("worker-A", max_count=1)
    engine._leases[(ClaimKind.SESSION, "se-dead-worker")].expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    )
    assert await engine.has_live_lease(ClaimKind.SESSION, "se-dead-worker") is False

    reaped = await _real_sweeper(storage, engine)._tick()

    assert reaped == 0
    assert (await storage.get("se-dead-worker")).status == SessionStatus.RUNNING
    (reclaimed,) = await engine.claim_due("worker-B", max_count=1)
    assert reclaimed.entity_id == "se-dead-worker"


@pytest.mark.asyncio
async def test_reaps_a_first_turn_whose_claim_was_lost(fake_storage_provider):
    """No lease row at all is what a lost claim looks like."""
    from primer.claim.in_memory import InMemoryClaimEngine

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-lost", age_seconds=3600, turn_no=0))

    reaped = await _real_sweeper(storage, InMemoryClaimEngine(adapters={}))._tick()

    assert reaped == 1
    assert (await storage.get("se-lost")).ended_detail == "never_started"


# ---------------------------------------------------------------------------------------------------------------------
# The terminal write is field-scoped (the no-whole-document-session-writer rule)
# ---------------------------------------------------------------------------------------------------------------------


class _EngineThatLetsAWriterIn(_FakeClaimEngine):
    """The lease lookup is the last await between the sweeper's read of the row and its write: the moment a concurrent writer
    (a worker parking the turn, a claim finishing it, a cancel ending it) gets in."""

    def __init__(self, in_the_gap) -> None:
        super().__init__(set())
        self._in_the_gap = in_the_gap

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        await self._in_the_gap()
        return await super().has_lease(kind, entity_id)


async def _sweep_with_a_writer_in_the_gap(storage, sid: str, change: dict) -> int:
    async def in_the_gap() -> None:
        row = await storage.get(sid)
        await storage.update(row.model_copy(update=change))

    return await StuckSessionSweeper(session_storage=storage, claim_engine=_EngineThatLetsAWriterIn(in_the_gap))._tick()


@pytest.mark.asyncio
async def test_the_sweeper_ends_a_row_without_a_whole_document_update(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-stuck"))

    async def whole_document_update(*args, **kwargs):
        raise AssertionError("the sweeper wrote the whole session row; it owns only status and the ended_* fields")

    storage.update = whole_document_update  # type: ignore[method-assign]

    assert await _sweeper(storage)._tick() == 1
    row = await storage.get("se-stuck")
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "failed", "never_started")
    assert row.ended_at is not None


@pytest.mark.asyncio
async def test_a_session_that_parks_while_the_sweeper_checks_its_lease_is_left_alone(fake_storage_provider):
    """A park committed between the read and the write must not be erased and then ended over: the write is fenced on the
    park columns the decision depended on."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-parks"))

    reaped = await _sweep_with_a_writer_in_the_gap(storage, "se-parks", {"parked_status": "parked"})

    row = await storage.get("se-parks")
    assert reaped == 0, "a session that parked in the gap was ended anyway"
    assert (row.status, row.parked_status, row.ended_reason) == (SessionStatus.RUNNING, "parked", None)


@pytest.mark.asyncio
async def test_a_session_whose_first_turn_completes_while_the_sweeper_checks_is_left_alone(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-ran"))

    reaped = await _sweep_with_a_writer_in_the_gap(storage, "se-ran", {"turn_no": 1})

    row = await storage.get("se-ran")
    assert reaped == 0, "a session whose first turn just completed was ended as never started"
    assert (row.status, row.turn_no, row.ended_reason) == (SessionStatus.RUNNING, 1, None)


@pytest.mark.asyncio
async def test_a_session_someone_else_ended_in_the_gap_keeps_their_reason(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-cancelled"))

    reaped = await _sweep_with_a_writer_in_the_gap(
        storage, "se-cancelled", {"status": SessionStatus.ENDED, "ended_reason": "cancelled"},
    )

    row = await storage.get("se-cancelled")
    assert reaped == 0
    assert (row.status, row.ended_reason, row.ended_detail) == (SessionStatus.ENDED, "cancelled", None), (
        "the first terminal reason wins: the sweeper must not rewrite why a session ended"
    )
