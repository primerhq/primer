"""Is a released lease claimable again at once on the in-memory claim engine when its kind says it is not due? (ticket 01a16e79)

One probe per registered claim kind (``ClaimKind``: SESSION, HARNESS, TRIGGER, TOOL_CALL; graph runs ride SESSION, and the scheduler is
not a claim kind), each on the real adapter over a real SQLite storage provider, the single-process deployment the in-memory engine
serves. Each probe runs a release the way the kind's producer does and asks ``claim_due`` whether the lease comes back, against what the
Postgres engine's claim query would answer: a lease is claimable there only when the kind's ``eligibility_sql`` holds for its entity.

* The ``due`` probes pin what must stay claimable (a resumed session's continuation, a retried tool call, a harness with an operation).
* The ``not due`` probes pin what the Postgres engine would not claim. Those the in-memory engine claims today are strict xfails
  (``raises=AssertionError``): they are the measured divergence, and they flip when the engine honours the kind's eligibility.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import primer.harness.dispatch as harness_dispatch
from primer.api._app_lifespan_phases import recover_sessions
from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.trigger import ScheduledTriggerConfig, Trigger
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.engine_handlers import run_engine_harness

_DIVERGES = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="the in-memory engine does not apply the kind's eligibility: the lease is claimed where Postgres would not claim it",
)


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


@_DIVERGES
async def test_session_a_lease_boot_recovery_arms_on_a_parked_session_is_not_claimed(sp, engine):
    """Not a release: ``recover_sessions`` (boot, in-memory deployments) arms a lease for every RUNNING session, and a park does not change
    ``status`` (``dispatch.py``: "A park doesn't touch session.status"), so a parked session gets one. Postgres would not claim it
    (``parked_status = 'parked'``); the in-memory engine does, and ``_run_engine_session`` then runs ``run_one_session_turn`` on it, not a
    resume (not exercised here)."""
    await sp.get_storage(WorkspaceSession).create(_session("s1", parked_status="parked", parked_at=_now()))

    await recover_sessions(engine, SimpleNamespace(enqueue=_noop), sp)

    assert await engine.has_lease(ClaimKind.SESSION, "s1"), "the probe's premise: boot recovery armed the parked session"
    assert await _claimable(engine, ClaimKind.SESSION) == []


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


@_DIVERGES
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


@_DIVERGES
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


@_DIVERGES
async def test_tool_call_a_task_failed_by_an_invalid_retry_release_is_not_claimed_again(sp, engine):
    """A retry release whose ``entity_update`` the branch refuses fails the task terminally (``_fail_rejected``) while the engine keeps
    the lease (a retry outcome does not drop it); the adapter's own docstring names this gap. Postgres would not claim a failed task."""
    lease = await _claimed_task(sp, engine, "t1")

    await engine.release(lease, outcome=ReleaseOutcome(
        success=False, last_error="transient", claim_token="tok-t1", entity_update={"result_state": {"x": 1}},
    ))

    assert (await sp.get_storage(ToolCallTask).get("t1")).state is ToolCallTaskState.FAILED
    assert await _claimable(engine, ClaimKind.TOOL_CALL) == []
