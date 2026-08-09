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
    """Just the one method the sweeper calls, over a set of leased session ids."""

    def __init__(self, leased: set[str] | None = None, *, raises: bool = False) -> None:
        self.leased = leased or set()
        self.raises = raises
        self.calls: list[tuple[ClaimKind, str]] = []

    async def has_live_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        self.calls.append((kind, entity_id))
        if self.raises:
            raise RuntimeError("lease table unreachable")
        return entity_id in self.leased


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
async def test_reaps_once_the_lease_expires(fake_storage_provider):
    """A worker that dies stops heartbeating, so its lease lapses and the row is reapable.

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
