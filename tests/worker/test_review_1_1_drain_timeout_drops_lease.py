"""Review 2026-11-23 finding 1.1: a turn cut by the drain timeout DELETEs its lease.

When ``drain_and_stop`` reaches its ``drain_timeout`` with a turn still running, it cancels the
turn's cancel scope with ``"worker_drain_timeout"``. For a SESSION turn the CancelledError arm in
``_run_engine_session`` (primer/worker/pool.py) re-raises WITHOUT touching the ``outcome`` variable,
so the handler's ``finally`` releases the lease with the DEFAULT
``ReleaseOutcome(success=False, drop_lease=True)``. The in-memory (or Postgres) engine then deletes
the lease and the session adapter's non-park branch clears the park columns, leaving the row
RUNNING with no lease and no park: no worker can ever claim it, and nothing re-arms it. The pool's
own comment on the resume-failure exit names the same end state as a bug ("leaves the row RUNNING
with no lease, forever"); the drain-timeout path reaches it for every session turn outlived by the
drain.

This test builds the real pool shape the unit tests use (``_FakeStorageProvider`` + a RUNNING
``WorkspaceSession`` row + ``InMemoryClaimEngine`` with the real ``SessionClaimAdapter``), claims
the session, runs its normal turn with ``run_one_session_turn`` patched to block, and calls
``drain_and_stop`` with a tiny timeout. After the drain and the turn's unwind, the session must
still be re-runnable: still RUNNING with a live lease a peer worker can re-claim. It must not be
RUNNING with no lease and no park.

Fixed (review 2026-11-23, 1.1; round 1): the pool's CancelledError arm recognises a cut the
drain made from the key ``drain_and_stop`` recorded (``_hand_back_keys``), not from the message of
whichever cancel arrives (the drain follows its cut with a plain cancel, see
``test_review_h2_round1.py``), and hands the lease back with
``ReleaseOutcome(success=False, entity_noop=True)`` - claimable again, row untouched - instead of
the default ``drop_lease=True`` release that ran the session adapter's non-park branch (park
cleared, terminal error record, RUNNING row with no lease and nothing to re-arm it).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider

WORKER = "wrk-drain-cut"
SID = "sess-drain-cut"


@pytest.mark.asyncio
async def test_a_turn_cut_by_the_drain_timeout_keeps_its_lease(monkeypatch):
    storage = _FakeStorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=storage,
        workspace_registry=None,          # type: ignore[arg-type]
        provider_registry=None,           # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER

    sess = WorkspaceSession(
        id=SID,
        workspace_id=f"ws-{SID}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=False,
    )
    await session_storage.create(sess)

    # Claim the session the way the pool's claim loop does.
    await engine.mark_resumable(ClaimKind.SESSION, SID)
    leases = await engine.claim_due(WORKER, max_count=10)
    lease = next(
        l for l in leases
        if l.kind == ClaimKind.SESSION and l.entity_id == SID
    )

    # A normal turn that outlives the drain timeout.
    blocked = asyncio.Event()
    never = asyncio.Event()

    async def _slow_turn(_lease, _deps):
        blocked.set()
        await never.wait()

    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", _slow_turn)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5.0)
        assert (lease.kind, lease.entity_id) in pool._active_scopes, \
            "the cancel scope must be registered while the turn is in flight"

        # The drain: the tiny timeout expires while the turn is blocked, so
        # drain_and_stop cancels the scope with "worker_drain_timeout".
        await pool.drain_and_stop(timeout=1.0)
        await asyncio.wait_for(task, timeout=10.0)

        # The turn was cut, not finished: the session must stay re-runnable.
        row = await session_storage.get(SID)
        assert row is not None, "the session row disappeared during the drain"
        assert row.status == SessionStatus.RUNNING, (
            f"the drain-timeout cut left the session {row.status.value!r}"
        )
        assert await engine.has_lease(ClaimKind.SESSION, SID), (
            "the drain-timeout cut released the lease (the default "
            "ReleaseOutcome(success=False, drop_lease=True)): the session is "
            "RUNNING with no lease and no park, so no worker can re-claim and "
            "re-run the cut turn"
        )
    finally:
        never.set()
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_resume_cut_by_the_drain_timeout_keeps_the_answer(monkeypatch):
    """A RESUME cut by the drain must not consume the person's answer.

    The answer sits on the row's park (parked_status="resumable", the answer in
    parked_state). The pool's resumable branch runs the resume handler; a drain
    cut of that handler must release with entity_noop - the row left EXACTLY as
    it was - so the next claim takes the same resumable row and resumes with the
    answer. Before the fix the default release ran the session adapter's
    non-park branch, which clears the park columns: the answer was erased before
    any worker could resume with it (review 2026-11-23, 1.1).
    """
    sid = "sess-drain-cut-resume"
    storage = _FakeStorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=storage,
        workspace_registry=None,          # type: ignore[arg-type]
        provider_registry=None,           # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER

    answer = "the person's answer to the parked question"
    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=now,
        turn_no=1,
        cancel_requested=False,
        parked_status="resumable",
        parked_at=now,
        parked_event_key="ask_user:call-1",
        parked_state={"kind": "ask_user", "resume_event_payload": {"answer": answer}},
    )
    await session_storage.create(sess)

    # Claim the session the way the pool's claim loop does.
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == sid
    )

    # A resume handler that outlives the drain timeout.
    blocked = asyncio.Event()
    never = asyncio.Event()

    async def _blocking_resume(_lease, _row):
        blocked.set()
        await never.wait()

    monkeypatch.setattr(pool, "_resume_engine_session", _blocking_resume)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5.0)
        assert (lease.kind, lease.entity_id) in pool._active_scopes, \
            "the cancel scope must be registered while the resume is in flight"

        await pool.drain_and_stop(timeout=1.0)
        await asyncio.wait_for(task, timeout=10.0)

        row = await session_storage.get(sid)
        assert row is not None
        assert row.status == SessionStatus.RUNNING
        assert row.parked_status == "resumable", (
            f"the drain-timeout cut of a resume cleared the park's status "
            f"({row.parked_status!r})"
        )
        assert (row.parked_state or {}).get("resume_event_payload", {}).get("answer") == answer, (
            "the drain-timeout cut of a resume erased the person's answer: the "
            "default release ran the adapter's non-park branch, which clears the park"
        )
        assert await engine.has_lease(ClaimKind.SESSION, sid), (
            "the cut resume's lease must stay so the next claim resumes with the answer"
        )

        # The next claim: a peer takes the same lease, and the answer is still
        # on the row for the resume branch to replay.
        peer = await engine.claim_due("wrk-peer", max_count=10)
        assert any(
            l.kind == ClaimKind.SESSION and l.entity_id == sid for l in peer
        ), "the peer cannot re-claim the cut resume"
        row = await session_storage.get(sid)
        assert (row.parked_state or {}).get("resume_event_payload", {}).get("answer") == answer, (
            "the answer was lost between the cut and the next claim"
        )
    finally:
        never.set()
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_turn_that_finishes_during_the_drain_releases_as_before(monkeypatch):
    """A turn that COMPLETES while the drain is still waiting is not cut.

    Its release takes the ordinary path - the successful outcome runs the
    adapter (turn_no bump) and the lease drops - exactly as before the fix.
    The drain-cut outcome (entity_noop, lease handed back claimable) must apply
    only to turns the drain actually cancels (review 2026-11-23, 1.1).
    """
    sid = "sess-drain-finished"
    storage = _FakeStorageProvider()
    session_storage = storage.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=storage,
        workspace_registry=None,          # type: ignore[arg-type]
        provider_registry=None,           # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER

    sess = WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
        cancel_requested=False,
    )
    await session_storage.create(sess)

    await engine.mark_resumable(ClaimKind.SESSION, sid)
    lease = next(
        l for l in await engine.claim_due(WORKER, max_count=10)
        if l.kind == ClaimKind.SESSION and l.entity_id == sid
    )

    # A turn that finishes long before the drain's cut.
    async def _quick_turn(_lease, _deps):
        await asyncio.sleep(0.1)
        return ReleaseOutcome(success=True, drop_lease=True)

    monkeypatch.setattr("primer.worker.pool.run_one_session_turn", _quick_turn)

    task = asyncio.create_task(pool._run_engine(lease, pool._run_engine_session))
    try:
        # The turn (0.1 s) finishes while the drain (5 s) waits, so the drain
        # never cancels the scope.
        await pool.drain_and_stop(timeout=5.0)
        await asyncio.wait_for(task, timeout=10.0)

        row = await session_storage.get(sid)
        assert row is not None
        assert row.status == SessionStatus.RUNNING
        # The ordinary successful release ran: the lease dropped and the bump
        # applied - not the drain-cut hand-back (which would have kept the lease).
        assert not await engine.has_lease(ClaimKind.SESSION, sid), (
            "a turn that finished during the drain must release its lease as before"
        )
        assert row.turn_no == 1, "the successful release's turn_no bump was not applied"
    finally:
        if not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
        await scheduler.aclose()
