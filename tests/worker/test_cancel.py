"""Tests for WorkerPool cancel/pause handling during a turn."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from matrix.model.scheduler import WorkerConfig
from matrix.model.session import (
    AgentSessionBinding,
    Session,
    SessionStatus,
)
from matrix.scheduler.in_memory import InMemoryScheduler
from matrix.worker.pool import WorkerPool


async def _async_return(v):
    return v


class _NoopPersist:
    async def persist_turn(self, turn_no): return None


class _SleepingExecutor:
    """Executor whose invoke() sleeps until cancelled."""

    async def invoke(self, _messages):
        await asyncio.sleep(60)


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


async def test_cancel_during_turn_transitions_to_ended_when_cancel_requested(
    scheduler, monkeypatch,
):
    """Mid-turn cancel: scope.cancel() fires, executor's sleep is
    interrupted, _run_one_turn lands in _handle_cancel and (because
    cancel_requested=True on the row) ends the session."""
    sid = "sess-cancel-mid-1"
    scheduler.register_session_for_test(sid)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,         # type: ignore[arg-type]
        workspace_registry=None,                   # type: ignore[arg-type]
        provider_registry=None,                    # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
        cancel_requested=True,    # set BEFORE the turn starts; same path the
                                  #   API takes when it sets the flag
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_SleepingExecutor()))

    # Note: cancel_requested is read pre-turn by _run_one_turn (Task 16),
    # so this test verifies the early-exit path landed correctly. The
    # truly-mid-turn case is exercised by the next test.
    await pool._run_one_turn(lease)
    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.ENDED


async def test_mid_turn_cancellation_via_scope(scheduler, monkeypatch):
    """If we cancel the run_one_turn task externally (mimicking the
    cancel-loop's scope.cancel call), _handle_cancel fires."""
    sid = "sess-mid-cancel-1"
    scheduler.register_session_for_test(sid)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,         # type: ignore[arg-type]
        workspace_registry=None,                   # type: ignore[arg-type]
        provider_registry=None,                    # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    # Note: cancel_requested STARTS False; we'll flip it after the
    # executor has been entered, then trigger the scope cancel.
    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_SleepingExecutor()))

    task = asyncio.create_task(pool._run_one_turn(lease))
    # Wait for the scope to be registered (sleep yields to the worker).
    for _ in range(50):
        if sid in pool._active_scopes:
            break
        await asyncio.sleep(0.01)
    assert sid in pool._active_scopes
    # Flip cancel_requested then trigger the scope (mirroring what the
    # cancel-loop does when NOTIFY arrives).
    fake_session.cancel_requested = True
    pool._active_scopes[sid].cancel("user_cancelled")

    # _run_one_turn re-raises CancelledError per the design (so the
    # outer claim-loop tracks the per-turn task as cancelled). Tolerate
    # that by suppressing here.
    try:
        await task
    except asyncio.CancelledError:
        pass

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.ENDED


async def test_mid_turn_pause_via_scope(scheduler, monkeypatch):
    """Same as cancel but with pause_requested -> status=PAUSED."""
    sid = "sess-mid-pause-1"
    scheduler.register_session_for_test(sid)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler, storage=None,         # type: ignore[arg-type]
        workspace_registry=None,                   # type: ignore[arg-type]
        provider_registry=None,                    # type: ignore[arg-type]
    )
    pool._worker_id = "wrk-test"
    await scheduler.register_worker(
        worker_id="wrk-test", host="h", pid=1, capacity=1,
    )
    await scheduler.enqueue(sid)
    [lease] = await scheduler.claim("wrk-test", max_count=1)

    fake_session = Session(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=lease.turn_no,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_SleepingExecutor()))

    task = asyncio.create_task(pool._run_one_turn(lease))
    for _ in range(50):
        if sid in pool._active_scopes:
            break
        await asyncio.sleep(0.01)
    assert sid in pool._active_scopes
    fake_session.pause_requested = True
    pool._active_scopes[sid].cancel("user_paused")

    try:
        await task
    except asyncio.CancelledError:
        pass

    snapshot = scheduler.session_snapshot_for_test(sid)
    assert snapshot.status == SessionStatus.PAUSED
