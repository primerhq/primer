"""H-2 fix round 1 (review 2026-11-23): the drain cut, the heartbeat hang and the self-cancel, on the real paths.

1. A cut turn whose unwind is slower than the drain's follow-up plain ``cancel()`` still hands the lease back
   claimable with the park and the person's answer kept: the outcome is decided from the key the drain recorded,
   not from the message of whichever cancel reaches the handler (finding 1). Driven through the pool's resume
   branch AND through the real ``run_one_session_turn`` over a real workspace.
2. A heartbeat that HANGS (a partition fails only at the command timeout, as long as the lease TTL) counts as a
   failure in time: the local turn is cancelled before a peer can claim the lease (finding 2), real timing.
3. The self-cancel hands the lease back instead of releasing with the default delete-the-lease outcome: the
   lease stays claimable and the park and the answer survive, and the next claim runs the turn (finding 3).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import pytest

import primer.worker.pool as pool_module
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.chat import Done, TextDelta
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool
from tests._support.off_golden import open_session
from tests.conftest import _FakeStorageProvider
from tests.worker.test_hard_cancel_lands_the_cancelled_exit import _Registry

ANSWER = "the person's answer"
SESSION_ID = "sess-h2r1"


def _engine(session_storage, **kwargs) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage)}, **kwargs,
    )


def _resumable_row(sid: str) -> WorkspaceSession:
    now = datetime.now(UTC)
    return WorkspaceSession(
        id=sid, workspace_id=f"ws-{sid}", binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING, created_at=now, turn_no=1, parked_status="resumable", parked_at=now,
        parked_event_key="k", parked_state={"kind": "ask_user", "resume_event_payload": {"answer": ANSWER}},
    )


async def _claim(engine, worker: str, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    return next(lease for lease in await engine.claim_due(worker, max_count=10) if lease.entity_id == sid)


async def _claimable_by_a_peer(engine, sid: str) -> bool:
    return any(lease.entity_id == sid for lease in await engine.claim_due("peer", max_count=10))


def _answer_of(row: WorkspaceSession | None):
    return ((row.parked_state or {}).get("resume_event_payload") or {}).get("answer") if row else None


# ---------------------------------------------------------------------------
# finding 1: a slow unwind outlives the drain's follow-up plain cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_cut_by_the_drain_keeps_park_answer_and_lease_when_its_unwind_is_slow():
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = _engine(sessions)
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=scheduler, storage=storage,
        workspace_registry=None, provider_registry=None, engine=engine,  # type: ignore[arg-type]
    )
    pool._worker_id = "wrk"
    await sessions.create(_resumable_row(SESSION_ID))
    lease = await _claim(engine, "wrk", SESSION_ID)
    blocked = asyncio.Event()
    cancels: list[tuple] = []

    async def resume(_lease, _row):
        blocked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancel:
            cancels.append(cancel.args)
            try:
                await asyncio.sleep(3.0)  # a cleanup slower than the drain's wait: the plain follow-up cancel lands in it
            except asyncio.CancelledError as again:
                cancels.append(again.args)
                raise
            raise

    pool._resume_engine_session = resume
    # Dispatched as the claim loop does, so the task is one the drain's follow-up plain cancel() reaches.
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
    pool._reserve_and_dispatch([lease])
    task = next(iter(pool._turn_tasks))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        await asyncio.wait_for(pool.drain_and_stop(timeout=1.0), 20)
        await asyncio.wait_for(task, 10)
        assert cancels == [("worker_drain_timeout",), ()], f"the unwind must have met the plain follow-up cancel: {cancels}"
        row = await sessions.get(SESSION_ID)
        assert row.parked_status == "resumable", "the cut resume cleared the park"
        assert _answer_of(row) == ANSWER, "the cut resume erased the person's answer"
        assert await _claimable_by_a_peer(engine, SESSION_ID), "the lease must be handed back claimable"
    finally:
        with suppress(Exception, asyncio.CancelledError):
            task.cancel()
            await task
        await scheduler.aclose()


class _SlowUnwind:
    """Streams one delta then blocks; cleaning up after a cancel takes longer than the drain's follow-up cancel."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.cleanup_cancelled_again = False

    async def invoke(self, messages, **kwargs):
        yield TextDelta(text="working", index=0)
        self.reached.set()
        try:
            await asyncio.Event().wait()
        finally:
            try:
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                self.cleanup_cancelled_again = True
                raise


class _FinishesNow:
    async def invoke(self, messages, **kwargs):
        yield TextDelta(text="done", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _real_turn_rig(tmp_path, *, executor, worker="wrk", scheduler=None, **engine_kwargs):
    backend, workspace, workspace_session = await open_session(tmp_path)
    sid = workspace_session.session_id
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=sid, workspace_id="w1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_no=0,
    ))
    engine = _engine(sessions, **engine_kwargs)
    scheduler = scheduler or InMemoryScheduler()
    await scheduler.initialize()

    def make_pool(name, ex, config=None):
        pool = WorkerPool(
            config=config or WorkerConfig(concurrency=1), scheduler=scheduler, storage=storage,
            workspace_registry=_Registry(workspace), provider_registry=None, engine=engine,  # type: ignore[arg-type]
        )
        pool._worker_id = name

        async def build(_session):
            return ex

        pool._build_session_executor = build
        return pool

    async def close():
        await scheduler.aclose()
        await workspace_session.aclose()
        await backend.aclose()

    return sid, sessions, engine, make_pool, close


@pytest.mark.asyncio
async def test_a_real_turn_cut_by_the_drain_keeps_its_lease_when_its_unwind_is_slow(tmp_path):
    executor = _SlowUnwind()
    sid, sessions, engine, make_pool, close = await _real_turn_rig(tmp_path, executor=executor)
    pool = make_pool("wrk", executor)
    lease = await _claim(engine, "wrk", sid)
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
    pool._reserve_and_dispatch([lease])
    task = next(iter(pool._turn_tasks))
    try:
        await asyncio.wait_for(executor.reached.wait(), 5)
        await asyncio.wait_for(pool.drain_and_stop(timeout=1.0), 20)
        await asyncio.wait_for(task, 10)
        assert executor.cleanup_cancelled_again, "the unwind must have met the plain follow-up cancel"
        row = await sessions.get(sid)
        assert row.status == SessionStatus.RUNNING
        assert await _claimable_by_a_peer(engine, sid), (
            "the slow unwind released with the default delete-the-lease outcome: RUNNING, no lease, nothing to re-arm it"
        )
    finally:
        with suppress(Exception, asyncio.CancelledError):
            task.cancel()
            await task
        await close()


# ---------------------------------------------------------------------------
# finding 2: a hanging heartbeat counts as a failure in time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hanging_heartbeat_cancels_the_local_turn_before_a_peer_can_claim(monkeypatch):
    """H = 1 s, TTL = 5 s, and every heartbeat hangs for the TTL (a partition fails only at the command timeout)."""
    ttl, hang = 5, 5.0
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = _engine(sessions, lease_ttl_seconds=ttl)
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=1, lease_ttl_seconds=ttl, poll_interval_seconds=0.1),
        scheduler=scheduler, storage=storage, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "A"
    await sessions.create(_resumable_row(SESSION_ID))
    lease = await _claim(engine, "A", SESSION_ID)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    cancelled_at: list[float] = []

    async def turn(_lease, _deps):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_at.append(loop.time() - t0)
            raise

    async def heartbeat_hangs(worker_id, kind_ids, **kwargs):
        await asyncio.sleep(hang)
        raise TimeoutError("command_timeout")

    monkeypatch.setattr(pool_module, "run_one_session_turn", turn)
    monkeypatch.setattr(engine, "heartbeat", heartbeat_hangs)
    # A normal turn on the row (the resumable park would take the resume branch).
    row = await sessions.get(SESSION_ID)
    row.parked_status = None
    await sessions.update(row)
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
    pool._reserve_and_dispatch([lease])
    # The two loops start() runs for this: the heartbeat (hung here) and the local watchdog.
    heartbeat = asyncio.create_task(pool._heartbeat_loop())
    watchdog = asyncio.create_task(pool._lease_watchdog())
    peer_at = None
    try:
        # What a peer's claim loop does: poll for the lease. The local cancel hands the lease back, so the peer
        # can only see it after it (or after the TTL, whichever comes first).
        while loop.time() - t0 < ttl + 4 and (peer_at is None or not cancelled_at):
            if peer_at is None and await engine.claim_due("B", max_count=1):
                peer_at = loop.time() - t0
            await asyncio.sleep(0.05)
        assert cancelled_at, "the hanging heartbeat never cancelled the local turn"
        assert peer_at is not None
        assert cancelled_at[0] < ttl, f"the local cancel landed at {cancelled_at[0]:.2f}s of a {ttl}s lease"
        assert cancelled_at[0] <= peer_at, (
            f"a peer claimed at {peer_at:.2f}s, before the local cancel at {cancelled_at[0]:.2f}s: a duplicate execution"
        )
    finally:
        pool._keepalive_done.set()
        heartbeat.cancel()
        watchdog.cancel()
        for task in list(pool._turn_tasks):
            task.cancel()
        await asyncio.sleep(0.1)
        await scheduler.aclose()


# ---------------------------------------------------------------------------
# finding 3: the self-cancel hands the lease back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_self_cancelled_resume_keeps_park_answer_and_a_claimable_lease():
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = _engine(sessions)
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
        scheduler=scheduler, storage=storage, workspace_registry=None, provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "A"
    await sessions.create(_resumable_row(SESSION_ID))
    lease = await _claim(engine, "A", SESSION_ID)

    async def fails(*_a, **_k):
        raise RuntimeError("heartbeat statement failed")

    engine.heartbeat = fails

    async def resume(_lease, _row):
        await asyncio.Event().wait()

    pool._resume_engine_session = resume
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
    pool._reserve_and_dispatch([lease])
    pool._lease_last_confirmed[(ClaimKind.SESSION, SESSION_ID)] -= 5.0  # unconfirmed past the bound
    heartbeat = asyncio.create_task(pool._heartbeat_loop())
    try:
        turn = next(iter(pool._turn_tasks))
        await asyncio.wait_for(asyncio.shield(turn), 6)
        row = await sessions.get(SESSION_ID)
        assert row.parked_status == "resumable", "the self-cancel cleared the park"
        assert _answer_of(row) == ANSWER, "the self-cancel erased the person's answer"
        assert row.status == SessionStatus.RUNNING
        assert await _claimable_by_a_peer(engine, SESSION_ID), "the self-cancel dropped the lease"
    finally:
        pool._keepalive_done.set()
        heartbeat.cancel()
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_self_cancelled_turn_leaves_the_session_runnable_by_the_next_claim(tmp_path):
    """The real turn path: turn_status stays "running" on the row (the 1.9 skip), and the next claim still runs it."""
    class _Blocks:
        def __init__(self) -> None:
            self.reached = asyncio.Event()

        async def invoke(self, messages, **kwargs):
            yield TextDelta(text="working", index=0)
            self.reached.set()
            await asyncio.Event().wait()

    blocks = _Blocks()
    sid, sessions, engine, make_pool, close = await _real_turn_rig(tmp_path, executor=blocks)
    config = WorkerConfig(concurrency=1, heartbeat_interval_seconds=1, lease_ttl_seconds=5)
    pool = make_pool("A", blocks, config)
    lease = await _claim(engine, "A", sid)

    async def fails(*_a, **_k):
        raise RuntimeError("heartbeat statement failed")

    engine.heartbeat = fails
    pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
    pool._reserve_and_dispatch([lease])
    heartbeat = asyncio.create_task(pool._heartbeat_loop())
    try:
        await asyncio.wait_for(blocks.reached.wait(), 5)
        pool._lease_last_confirmed[(ClaimKind.SESSION, sid)] -= 5.0
        turn = next(iter(pool._turn_tasks))
        await asyncio.wait_for(asyncio.shield(turn), 6)
        row = await sessions.get(sid)
        assert row.status == SessionStatus.RUNNING and row.turn_status == "running"
        assert await engine.has_lease(ClaimKind.SESSION, sid), "the self-cancel dropped the lease"
        # The lease is still claimable and a peer's claim runs the turn to its end: "running" never blocks a claim.
        peer = make_pool("B", _FinishesNow())
        peer_lease = await _claim(engine, "B", sid)
        await asyncio.wait_for(peer._run_engine(peer_lease, peer._run_engine_session), 10)
        after = await sessions.get(sid)
        assert after.turn_no == 1, f"the next claim did not run the turn (turn_no={after.turn_no})"
        assert after.turn_status == "idle", f"the next claim left turn_status={after.turn_status!r}"
    finally:
        pool._keepalive_done.set()
        heartbeat.cancel()
        with suppress(Exception, asyncio.CancelledError):
            for task in list(pool._turn_tasks):
                task.cancel()
        await close()
