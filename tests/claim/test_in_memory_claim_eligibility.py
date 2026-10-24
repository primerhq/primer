"""A released or armed lease is claimed on the in-memory engine only when its kind says the entity is due (ticket 01a16e79).

One probe per registered claim kind (``ClaimKind``: SESSION, HARNESS, TRIGGER, TOOL_CALL; graph runs ride SESSION, and the scheduler is
not a claim kind), each on the real adapter over a real SQLite storage provider, the single-process deployment the in-memory engine
serves. Each probe runs a release (or an arm) the way the kind's producer does and asks ``claim_due`` whether the lease comes back,
against what the Postgres engine's claim query answers: a lease is claimable there only when the kind's ``eligibility_sql`` holds for
its entity. ``claim_due`` now asks the adapter's twin, ``is_eligible``.

* The ``due`` probes pin what must stay claimable (a resumed session's continuation, a retried tool call, a harness with an operation).
* The ``not due`` probes pin what the Postgres engine would not claim. Four of them were red before the engine applied eligibility (the
  ticket's measurement): the boot-recovery lease of a parked session, a kept harness lease whose operation finished, a trigger released
  without a requeue, and a tool-call task failed by an invalid retry release.
* The engine contract: eligibility is decided before ``max_count``, a missing entity is never claimed, a kind with no adapter or an
  adapter with no storage stays claimable, an unreadable entity is skipped without failing the claim, and a row claimed or dropped
  while its entity was read is not claimed again.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import primer.harness.dispatch as harness_dispatch
from primer.api._app_lifespan_phases import recover_sessions
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimAdapter, ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.trigger import ScheduledTriggerConfig, Trigger
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.engine_handlers import run_engine_harness

def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "claims.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
def engine(sp) -> InMemoryClaimEngine:
    """The engine ``ClaimEngineFactory.create`` builds for an in-memory bus: every registered kind, on the deployment's storage."""
    return InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=sp.get_storage(WorkspaceSession)),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=sp.get_storage(Harness)),
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger)),
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=sp.get_storage(ToolCallTask)),
    })


async def _claim_one(engine: InMemoryClaimEngine, kind: ClaimKind, entity_id: str):
    [lease] = await engine.claim_due("w", max_count=10, kinds=[kind])
    assert lease.entity_id == entity_id
    return lease


async def _claimable(engine: InMemoryClaimEngine, kind: ClaimKind) -> list[str]:
    return [lease.entity_id for lease in await engine.claim_due("w2", max_count=10, kinds=[kind])]


# ---------------------------------------------------------------------------
# SESSION: eligible while parked_status is NULL or 'resumable'
# ---------------------------------------------------------------------------


def _session(sid: str, **extra) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=_now(), turn_no=1, **extra,
    )


def _park() -> ParkRequest:
    now = _now()
    return ParkRequest(
        parked_state={"kind": "approval"}, parked_event_key="tool_approval:s:t1", parked_until=now + timedelta(hours=1), parked_at=now,
    )


async def test_session_a_turn_that_rests_waiting_is_not_claimed_again(sp, engine):
    """``run_one_session_turn``'s clean exit: the lease is dropped."""
    await sp.get_storage(WorkspaceSession).create(_session("s1"))
    await engine.upsert(ClaimKind.SESSION, "s1")
    lease = await _claim_one(engine, ClaimKind.SESSION, "s1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert await _claimable(engine, ClaimKind.SESSION) == []


async def test_session_a_parked_turn_is_not_claimed_again(sp, engine):
    """The park exit (``dispatch.py`` and every re-park of the resume coordinators): the lease is dropped with the park."""
    await sp.get_storage(WorkspaceSession).create(_session("s1"))
    await engine.upsert(ClaimKind.SESSION, "s1")
    lease = await _claim_one(engine, ClaimKind.SESSION, "s1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True, park=_park()))

    assert (await sp.get_storage(WorkspaceSession).get("s1")).parked_status == "parked"
    assert await _claimable(engine, ClaimKind.SESSION) == []


async def test_session_a_resumed_continuation_is_claimed_again_at_once(sp, engine):
    """Due: the resume handlers' continuation (``ReleaseOutcome(success=True, drop_lease=False)``) clears the park and keeps the lease,
    so the next claim runs the continuation turn. Eligible on Postgres too (``parked_status`` is NULL again)."""
    await sp.get_storage(WorkspaceSession).create(_session("s1", parked_status="resumable", parked_at=_now()))
    await engine.mark_resumable(ClaimKind.SESSION, "s1")
    lease = await _claim_one(engine, ClaimKind.SESSION, "s1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=False))

    assert (await sp.get_storage(WorkspaceSession).get("s1")).parked_status is None
    assert await _claimable(engine, ClaimKind.SESSION) == ["s1"]


async def test_session_boot_recovery_over_a_parked_running_session_claims_no_lease_and_runs_no_turn(sp, engine):
    """Not a release: ``recover_sessions`` (boot) arms a lease for every RUNNING session, and a park does not change ``status``
    (``dispatch.py``: "A park doesn't touch session.status"), so a parked session gets one. Before the fix the in-memory engine claimed
    it and ``_run_engine_session`` ran ``run_one_session_turn`` over the park (a fresh turn, a graph from its begin node). Now the lease
    waits, as on Postgres, until the resume event flips the row and re-arms it."""
    from primer.model.scheduler import WorkerConfig
    from primer.worker.pool import WorkerPool

    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_session("s1", parked_status="parked", parked_at=_now()))

    await recover_sessions(engine, SimpleNamespace(enqueue=_noop), sp)

    assert await engine.has_lease(ClaimKind.SESSION, "s1"), "the premise: boot recovery armed the parked session"
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=SimpleNamespace(enqueue=_noop),  # type: ignore[arg-type]
        storage=sp, workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    turns: list[str] = []

    async def _turn(lease, deps):
        turns.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    with patch("primer.worker.pool.run_one_session_turn", side_effect=_turn):
        with pytest.raises(RuntimeError, match="no runnable lease"):
            await pool.run_one_turn_now("s1")
    assert turns == [], "a fresh turn ran over the park"
    assert await engine.has_lease(ClaimKind.SESSION, "s1"), "the armed lease is kept for the resume"

    # The resume event flips the row and re-arms the lease (durably_mark_session_resumable): now it is claimed.
    current = await sessions.get("s1")
    await sessions.update(current.model_copy(update={"parked_status": "resumable"}))
    await engine.mark_resumable(ClaimKind.SESSION, "s1")
    assert await _claimable(engine, ClaimKind.SESSION) == ["s1"]


async def _noop(*_args, **_kwargs) -> None:
    return None


# ---------------------------------------------------------------------------
# HARNESS: eligible while pending_operation is set
# ---------------------------------------------------------------------------


def _harness(hid: str, *, pending: HarnessOperation | None) -> Harness:
    return Harness(id=hid, slug="hh", name="h", status=HarnessStatus.READY, pending_operation=pending, created_at=_now())


async def test_harness_a_finished_operation_is_not_claimed_again(sp, engine, monkeypatch):
    """The worker's harness handler: the operation runs (its dispatch clears ``pending_operation``) and the lease is dropped."""
    await sp.get_storage(Harness).create(_harness("h1", pending=HarnessOperation.SYNC))
    await engine.upsert(ClaimKind.HARNESS, "h1")
    lease = await _claim_one(engine, ClaimKind.HARNESS, "h1")

    async def _operation(deps, *, harness_id, worker_id):
        await harness_dispatch._release_harness(sp.get_storage(Harness), harness_id, worker_id, next_status=HarnessStatus.READY)

    monkeypatch.setattr(harness_dispatch, "run_one_harness_operation", _operation)

    async def _release(lease, outcome) -> None:
        await engine.release(lease, outcome=outcome)

    pool = SimpleNamespace(
        _storage=sp, _engine=engine, _event_bus=None, _provider_registry=None, _semantic_search_registry=None, _worker_id="w",
        _release_lease=_release,
    )
    await run_engine_harness(pool, lease)  # type: ignore[arg-type]

    assert await _claimable(engine, ClaimKind.HARNESS) == []


async def test_harness_a_kept_lease_with_an_operation_pending_is_claimed_again(sp, engine):
    """Due: a lease handed back with its operation still pending (``entity_noop``: the drain hand-back) is claimable."""
    await sp.get_storage(Harness).create(_harness("h1", pending=HarnessOperation.SYNC))
    await engine.upsert(ClaimKind.HARNESS, "h1")
    lease = await _claim_one(engine, ClaimKind.HARNESS, "h1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True, entity_noop=True))

    assert await _claimable(engine, ClaimKind.HARNESS) == ["h1"]


async def test_harness_a_kept_lease_whose_operation_is_finished_is_not_claimed(sp, engine):
    """No production harness release keeps its lease (every one drops it); this is the engine contract: a failed release that keeps the
    lease finalizes the row (``on_release`` clears ``pending_operation``), so Postgres would not claim it again."""
    await sp.get_storage(Harness).create(_harness("h1", pending=HarnessOperation.SYNC))
    await engine.upsert(ClaimKind.HARNESS, "h1")
    lease = await _claim_one(engine, ClaimKind.HARNESS, "h1")

    await engine.release(lease, outcome=ReleaseOutcome(success=False, last_error="boom"))

    assert (await sp.get_storage(Harness).get("h1")).pending_operation is None
    assert await _claimable(engine, ClaimKind.HARNESS) == []


# ---------------------------------------------------------------------------
# TRIGGER: eligible while enabled, time-based and next_fire_at <= now
# ---------------------------------------------------------------------------
#
# The production release (``run_engine_trigger`` after #915) requeues the lease to the next tick or drops it, and
# ``tests/trigger/test_trigger_claim_release.py`` pins that on SQLite. The probe below is the release WITHOUT that requeue, the one main
# shipped before #915: the adapter advances ``next_fire_at`` past now, so Postgres would not claim it until then.


async def test_trigger_a_release_without_a_requeue_is_not_claimed_before_its_next_tick(sp, engine):
    await sp.get_storage(Trigger).create(Trigger(
        id="tr1", slug="daily", name="d", config=ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC"), enabled=True,
        next_fire_at=_now() - timedelta(minutes=1), created_at=_now() - timedelta(days=2),
    ))
    await engine.upsert(ClaimKind.TRIGGER, "tr1")
    lease = await _claim_one(engine, ClaimKind.TRIGGER, "tr1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True))

    assert (await sp.get_storage(Trigger).get("tr1")).next_fire_at > _now()
    assert await _claimable(engine, ClaimKind.TRIGGER) == []


async def test_trigger_a_due_lease_is_claimed_at_its_tick_and_not_a_moment_before(sp):
    """The ``next_fire_at <= now()`` boundary, on the engine's clock: a lease armed due now (an early arm) waits for the tick."""
    tick = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    clock = {"now": tick - timedelta(microseconds=1)}
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger))}, clock=lambda: clock["now"],
    )
    await sp.get_storage(Trigger).create(Trigger(
        id="tr1", slug="daily", name="d", config=ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC"), enabled=True,
        next_fire_at=tick, created_at=tick - timedelta(days=2),
    ))
    await engine.upsert(ClaimKind.TRIGGER, "tr1")

    assert await _claimable(engine, ClaimKind.TRIGGER) == [], "claimed before its tick"
    clock["now"] = tick
    assert await _claimable(engine, ClaimKind.TRIGGER) == ["tr1"]


# ---------------------------------------------------------------------------
# TOOL_CALL: eligible while state is queued or running (no production claimer: WorkerPool registers no TOOL_CALL handler)
# ---------------------------------------------------------------------------


def _task(tid: str) -> ToolCallTask:
    return ToolCallTask(
        id=tid, session_id="sess-1", turn_no=0, tool_name="workspace__write", state=ToolCallTaskState.RUNNING, record_seq=1,
        claim_token=f"tok-{tid}", created_at=_now(), started_at=_now(),
    )


async def _claimed_task(sp, engine, tid: str):
    await sp.get_storage(ToolCallTask).create(_task(tid))
    await engine.upsert(ClaimKind.TOOL_CALL, tid)
    return await _claim_one(engine, ClaimKind.TOOL_CALL, tid)


async def test_tool_call_a_finished_task_is_not_claimed_again(sp, engine):
    lease = await _claimed_task(sp, engine, "t1")

    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True, claim_token="tok-t1"))

    assert await _claimable(engine, ClaimKind.TOOL_CALL) == []


async def test_tool_call_a_retried_task_is_claimed_again_at_once(sp, engine):
    """Due: the retry branch requeues the task (state back to queued) and keeps the lease."""
    lease = await _claimed_task(sp, engine, "t1")

    await engine.release(lease, outcome=ReleaseOutcome(success=False, last_error="transient", claim_token="tok-t1"))

    assert (await sp.get_storage(ToolCallTask).get("t1")).state is ToolCallTaskState.QUEUED
    assert await _claimable(engine, ClaimKind.TOOL_CALL) == ["t1"]


async def test_tool_call_a_task_failed_by_an_invalid_retry_release_is_not_claimed_again(sp, engine):
    """A retry release whose ``entity_update`` the branch refuses fails the task terminally (``_fail_rejected``) while the engine keeps
    the lease (a retry outcome does not drop it). Postgres would not claim a failed task, and now neither does this engine."""
    lease = await _claimed_task(sp, engine, "t1")

    await engine.release(lease, outcome=ReleaseOutcome(
        success=False, last_error="transient", claim_token="tok-t1", entity_update={"result_state": {"x": 1}},
    ))

    assert (await sp.get_storage(ToolCallTask).get("t1")).state is ToolCallTaskState.FAILED
    assert await _claimable(engine, ClaimKind.TOOL_CALL) == []


# ---------------------------------------------------------------------------
# The engine contract
# ---------------------------------------------------------------------------


class _Adapter(ClaimAdapter):
    """A test adapter whose eligibility is a set of ids, with an optional gate the read waits on."""

    kind = ClaimKind.HARNESS
    entity_table = "harness"

    def __init__(self, eligible: set[str], *, gate: asyncio.Event | None = None, broken: frozenset[str] = frozenset()) -> None:
        self.eligible = eligible
        self.gate = gate
        self.broken = broken
        self.read: list[str] = []

    def eligibility_sql(self) -> str:
        return "TRUE"

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        self.read.append(entity_id)
        if self.gate is not None:
            await self.gate.wait()
        if entity_id in self.broken:
            raise RuntimeError("unreadable row")
        return entity_id in self.eligible

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        return None


async def _suspended_in_the_read(adapter: _Adapter, claim: asyncio.Task) -> None:
    """Wait (bounded) until ``claim`` is suspended inside the adapter's read; an engine that never reads fails here, it does not hang."""
    async with asyncio.timeout(5):
        while adapter.read != ["h1"]:
            assert not claim.done(), f"the claim finished without reading the entity: {claim.result()}"
            await asyncio.sleep(0)


async def test_engine_decides_eligibility_before_max_count():
    """An ineligible lease ahead in priority does not use up the claim's slots (the query filters before its LIMIT)."""
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter({"ok"})})
    await engine.upsert(ClaimKind.HARNESS, "not-due", priority=10)
    await engine.upsert(ClaimKind.HARNESS, "ok", priority=100)

    assert [lease.entity_id for lease in await engine.claim_due("w", max_count=1)] == ["ok"]
    assert await engine.has_lease(ClaimKind.HARNESS, "not-due"), "an ineligible lease is kept, unclaimed, as on Postgres"


async def test_engine_never_claims_a_lease_whose_entity_is_missing(engine):
    for kind in ClaimKind:
        await engine.upsert(kind, f"gone-{kind.value}")

    assert await engine.claim_due("w", max_count=10) == []


async def test_engine_claims_a_kind_with_no_adapter_or_an_adapter_with_no_storage():
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(session_storage=None),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
        ClaimKind.TRIGGER: TriggerClaimAdapter(storage=None),
    })
    for kind in ClaimKind:                      # TOOL_CALL has no adapter here at all
        await engine.upsert(kind, f"x-{kind.value}")

    assert {lease.kind for lease in await engine.claim_due("w", max_count=10)} == set(ClaimKind)


async def test_engine_skips_an_entity_it_cannot_read_and_claims_the_rest(caplog):
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter({"ok", "bad"}, broken=frozenset({"bad"}))})
    await engine.upsert(ClaimKind.HARNESS, "bad", priority=10)
    await engine.upsert(ClaimKind.HARNESS, "ok", priority=100)

    with caplog.at_level(logging.WARNING, logger="primer.claim.in_memory"):
        assert [lease.entity_id for lease in await engine.claim_due("w", max_count=10)] == ["ok"]
    assert "could not read harness/bad" in caplog.text
    assert await engine.has_lease(ClaimKind.HARNESS, "bad"), "the next poll asks again"


async def test_engine_does_not_claim_a_row_another_claim_took_while_its_entity_was_read():
    """Two claims in one loop: the first is suspended in the entity read, the second claims the row; the first must not claim it too."""
    gate = asyncio.Event()
    adapter = _Adapter({"h1"}, gate=gate)
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: adapter})
    await engine.upsert(ClaimKind.HARNESS, "h1")

    first = asyncio.create_task(engine.claim_due("w1", max_count=1))
    await _suspended_in_the_read(adapter, first)
    adapter.gate = None
    [taken] = await engine.claim_due("w2", max_count=1)
    gate.set()

    assert await first == [], "the row was claimed twice"
    assert taken.claimed_by == "w2"


async def test_engine_does_not_claim_a_row_dropped_while_its_entity_was_read():
    gate = asyncio.Event()
    adapter = _Adapter({"h1"}, gate=gate)
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: adapter})
    await engine.upsert(ClaimKind.HARNESS, "h1")

    first = asyncio.create_task(engine.claim_due("w1", max_count=1))
    await _suspended_in_the_read(adapter, first)
    await engine.delete_lease(ClaimKind.HARNESS, "h1")
    gate.set()

    assert await first == []
    assert not await engine.has_lease(ClaimKind.HARNESS, "h1")
