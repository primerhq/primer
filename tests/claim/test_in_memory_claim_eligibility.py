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
  adapter with no storage stays claimable, an unreadable entity is skipped without failing the claim (logged once per row and error
  type, counted every time) while a bug in an adapter propagates, a row written to while its entity was read is not claimed on that
  read, and a lease's stamps are read from the clock when they are written (review of #928, round 2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import primer.harness.dispatch as harness_dispatch
import primer.observability.metrics as metrics
from primer.api._app_lifespan_phases import recover_sessions
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimAdapter, ClaimKind, ParkRequest, ReleaseOutcome, read_for_eligibility
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


async def test_trigger_the_twin_reads_a_naive_next_fire_at_as_utc():
    """Probe p1 of the review: ``naive <= aware`` raised in the twin where the SQL casts the value. The model normalises a naive
    time on read now; the twin holds for a model that did not (a copy made without validation)."""
    tick = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    stored = Trigger(
        id="tr1", slug="daily", name="d", config=ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC"), enabled=True,
        next_fire_at=tick, created_at=tick - timedelta(days=2),
    ).model_copy(update={"next_fire_at": tick.replace(tzinfo=None)})
    assert stored.next_fire_at.tzinfo is None, "the premise: a naive value reaches the twin"

    class _Storage:
        async def get(self, entity_id, *, conn=None):
            return stored

    adapter = TriggerClaimAdapter(storage=_Storage())
    assert await adapter.is_eligible("tr1", tick - timedelta(microseconds=1)) is False
    assert await adapter.is_eligible("tr1", tick) is True


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


class _FailingStorage:
    """A storage whose read raises ``error``: what a backend raises for a row it cannot read or decode."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def get(self, entity_id, *, conn=None):
        raise self.error


class _Adapter(ClaimAdapter):
    """A test adapter whose eligibility is a set of ids, with an optional gate the read waits on.

    ``failing`` maps an id to the error its storage read raises; the read goes through ``read_for_eligibility`` as a shipped
    adapter's does.
    """

    kind = ClaimKind.HARNESS
    entity_table = "harness"

    def __init__(
        self, eligible: set[str], *, gate: asyncio.Event | None = None, failing: dict[str, BaseException] | None = None,
    ) -> None:
        self.eligible = eligible
        self.gate = gate
        self.failing = failing if failing is not None else {}
        self.read: list[str] = []

    def eligibility_sql(self) -> str:
        return "TRUE"

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        self.read.append(entity_id)
        if self.gate is not None:
            await self.gate.wait()
        if entity_id in self.failing:
            await read_for_eligibility(_FailingStorage(self.failing[entity_id]), entity_id)
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
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter({"ok", "bad"}, failing={"bad": OSError("disk")})})
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


# ---------------------------------------------------------------------------
# Review of #928, round 2: an unreadable row is logged once and counted; a bug in an adapter propagates
# ---------------------------------------------------------------------------


def _read_errors(kind: str) -> float:
    return metrics.claim_eligibility_read_errors_total.labels(kind)._value.get()


def _warnings_for(caplog, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and needle in r.getMessage()]


async def _set_session_json(sp, sid: str, *pairs: str) -> None:
    """Edit the stored document directly (``json_set`` path/value pairs), the way a hand edit or an older build leaves it."""
    placeholders = ", ".join("?, ?" for _ in range(len(pairs) // 2))
    await sp.connection.execute(f"UPDATE sessions SET data = json_set(data, {placeholders}) WHERE id = ?", (*pairs, sid))
    await sp.connection.commit()


async def test_an_undecodable_row_is_logged_once_with_its_traceback_counted_every_poll_and_logged_again_after_a_good_read(
    sp, engine, caplog,
):
    """Probe p3 of the review: one WARNING per poll per bad row, for ever. Now: logged once per row and error type, with the
    traceback, until a read of the row succeeds; counted on every failed read."""
    await sp.get_storage(WorkspaceSession).create(_session("s-bad"))
    await engine.upsert(ClaimKind.SESSION, "s-bad")
    await _set_session_json(sp, "s-bad", "$.status", "bogus")
    before = _read_errors("session")

    with caplog.at_level(logging.WARNING, logger="primer.claim.in_memory"):
        for _ in range(5):
            assert await engine.claim_due("w", max_count=5) == []
        first = _warnings_for(caplog, "could not read session/s-bad")
        assert len(first) == 1, "logged once, not once per poll"
        assert first[0].exc_info is not None, "the first line carries the traceback"
        assert _read_errors("session") - before == 5, "counted on every failed read"

        await _set_session_json(sp, "s-bad", "$.status", "running", "$.parked_status", "parked")   # readable, not eligible
        assert await engine.claim_due("w", max_count=5) == []
        await _set_session_json(sp, "s-bad", "$.status", "bogus")                                  # unreadable again
        assert await engine.claim_due("w", max_count=5) == []

    assert len(_warnings_for(caplog, "could not read session/s-bad")) == 2, "a good read in between: the next failure is logged"
    assert _read_errors("session") - before == 6
    assert await engine.has_lease(ClaimKind.SESSION, "s-bad")


async def test_an_unreadable_row_is_logged_again_for_a_different_error_type(caplog):
    adapter = _Adapter(set(), failing={"h1": OSError("disk")})
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: adapter})
    await engine.upsert(ClaimKind.HARNESS, "h1")

    with caplog.at_level(logging.WARNING, logger="primer.claim.in_memory"):
        await engine.claim_due("w", max_count=1)
        await engine.claim_due("w", max_count=1)
        adapter.failing["h1"] = ValueError("bad document")
        await engine.claim_due("w", max_count=1)
        await engine.claim_due("w", max_count=1)

    logged = _warnings_for(caplog, "could not read harness/h1")
    assert [r.exc_info[1].__cause__.__class__ for r in logged] == [OSError, ValueError]


async def test_an_adapter_without_is_eligible_is_a_bug_that_propagates_and_is_not_counted():
    """Probe p3b of the review: a duck-typed adapter read as "could not read harness/h1" and its kind was never claimed again."""
    class _Duck:
        kind = ClaimKind.HARNESS
        entity_table = "harness"

        def eligibility_sql(self) -> str:
            return "TRUE"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Duck()})  # type: ignore[dict-item]
    await engine.upsert(ClaimKind.HARNESS, "h1")
    before = _read_errors("harness")

    with pytest.raises(AttributeError):
        await engine.claim_due("w", max_count=5)
    assert _read_errors("harness") == before


async def test_a_type_error_raised_by_an_adapters_own_logic_propagates():
    class _Comparing(_Adapter):
        async def is_eligible(self, entity_id: str, now: datetime) -> bool:
            return datetime(2026, 1, 1) <= now      # naive against aware: the adapter's bug, not an unreadable row

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Comparing(set())})
    await engine.upsert(ClaimKind.HARNESS, "h1")

    with pytest.raises(TypeError):
        await engine.claim_due("w", max_count=5)


# ---------------------------------------------------------------------------
# Review of #928, round 2: a decision read before a write to the row is not acted on
# ---------------------------------------------------------------------------


class _FlipAdapter(ClaimAdapter):
    """Decides from the entity as read, then resumes later (the SQLite read runs in its thread); its release finishes the entity."""

    kind = ClaimKind.HARNESS
    entity_table = "harness"

    def __init__(self) -> None:
        self.eligible = {"h1"}
        self.gate: asyncio.Event | None = None
        self.read: list[str] = []

    def eligibility_sql(self) -> str:
        return "TRUE"

    async def is_eligible(self, entity_id: str, now: datetime) -> bool:
        self.read.append(entity_id)
        decided = entity_id in self.eligible
        if self.gate is not None:
            await self.gate.wait()
        return decided

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        self.eligible.discard(entity_id)


async def test_a_claim_does_not_act_on_eligibility_read_before_an_expired_holders_release():
    """Probe p4 of the review. A's lease expired while A still runs; B's claim reads the entity (eligible) and is suspended; A
    releases, keeping the lease, and the release finishes the entity. B resumes on a decision about an entity that has changed:
    it must not claim (the row's generation moved), and a fresh claim on the same state does not claim either."""
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    adapter = _FlipAdapter()
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: adapter}, clock=lambda: clock["now"])
    await engine.upsert(ClaimKind.HARNESS, "h1")
    [held] = await engine.claim_due("A", max_count=1)
    clock["now"] += timedelta(seconds=engine.lease_ttl_seconds + 1)

    adapter.gate = asyncio.Event()
    second = asyncio.create_task(engine.claim_due("B", max_count=1))
    async with asyncio.timeout(5):
        while adapter.read != ["h1", "h1"]:
            assert not second.done(), "the claim finished without reading the entity"
            await asyncio.sleep(0)
    await engine.release(held, outcome=ReleaseOutcome(success=False, last_error="boom"))
    assert "h1" not in adapter.eligible
    adapter.gate.set()

    assert await second == [], "B claimed on the eligibility it read before A's release"
    adapter.gate = None
    assert await engine.claim_due("C", max_count=1) == []


@pytest.mark.parametrize("write", ["upsert", "mark_resumable"])
async def test_a_claim_does_not_act_on_a_read_the_row_was_rearmed_during(write):
    """An upsert that changes only the priority, or a ``mark_resumable``, during the read: the row moved, so that read is not
    acted on; the next claim reads it again and claims."""
    gate = asyncio.Event()
    adapter = _Adapter({"h1"}, gate=gate)
    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: adapter})
    await engine.upsert(ClaimKind.HARNESS, "h1")

    first = asyncio.create_task(engine.claim_due("w1", max_count=1))
    await _suspended_in_the_read(adapter, first)
    if write == "upsert":
        await engine.upsert(ClaimKind.HARNESS, "h1", priority=10)
    else:
        await engine.mark_resumable(ClaimKind.HARNESS, "h1")
    gate.set()

    assert await first == []
    adapter.gate = None
    assert [lease.entity_id for lease in await engine.claim_due("w2", max_count=1)] == ["h1"]


async def test_the_lease_stamps_are_read_from_the_clock_when_they_are_written():
    """The due and eligibility decision is made at the claim's start; ``claimed_at`` and ``expires_at`` are the clock's time when
    the claim is stamped, after the entity read (the review: a slow read made a lease expire early by the time it took)."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"now": start}

    class _SlowRead(_Adapter):
        async def is_eligible(self, entity_id: str, now: datetime) -> bool:
            assert now == start, "the decision is made at the claim's start"
            clock["now"] = start + timedelta(seconds=7)
            return True

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _SlowRead(set())}, clock=lambda: clock["now"])
    await engine.upsert(ClaimKind.HARNESS, "h1")

    [lease] = await engine.claim_due("w", max_count=1)

    stamped = start + timedelta(seconds=7)
    assert (lease.claimed_at, lease.expires_at) == (stamped, stamped + timedelta(seconds=engine.lease_ttl_seconds))
    row = engine._leases[(ClaimKind.HARNESS, "h1")]
    assert (row.claimed_at, row.last_heartbeat_at, row.expires_at) == (lease.claimed_at, stamped, lease.expires_at)


# ---------------------------------------------------------------------------
# Review of #928, round 2: boot recovery through the REAL claim loop
# ---------------------------------------------------------------------------


async def test_session_boot_recovery_over_a_parked_running_session_runs_no_turn_through_the_real_claim_loop(sp, engine):
    """Probe p6 of the review, next to the ``run_one_turn_now`` pin above: the pool's own claim loop (``start()``, a 0.1 s poll)
    polls the parked session's armed lease for a second and runs no turn; the lease is kept for the resume. Red before the fix
    (the loop ran a turn over the park)."""
    from primer.model.scheduler import WorkerConfig
    from primer.scheduler.in_memory import InMemoryScheduler
    from primer.worker.pool import WorkerPool

    await sp.get_storage(WorkspaceSession).create(
        _session("s1", parked_status="parked", parked_at=_now(), parked_event_key="evt:1"),
    )
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    await recover_sessions(engine, scheduler, sp)
    assert await engine.has_lease(ClaimKind.SESSION, "s1"), "the premise: boot recovery armed the parked session"

    turns: list[str] = []

    async def _turn(lease, deps):
        turns.append(lease.entity_id)
        return ReleaseOutcome(success=True, drop_lease=True)

    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=2, claim_batch_size=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5,
            poll_interval_seconds=0.1, drain_timeout_seconds=5,
        ),
        scheduler=scheduler, storage=sp, workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    try:
        with patch("primer.worker.pool.run_one_session_turn", side_effect=_turn):
            await pool.start()
            try:
                await asyncio.sleep(1.0)                     # about ten polls of the real loop
                empty_polls = pool.metrics_snapshot()["primer_worker_claims_empty_total"]
            finally:
                await pool.drain_and_stop(timeout=2)
    finally:
        await scheduler.aclose()

    assert turns == [], "the real claim loop ran a turn over the park"
    assert empty_polls >= 3, "the loop really polled"
    assert await engine.has_lease(ClaimKind.SESSION, "s1"), "the armed lease is kept for the resume"
