"""Tests for WorkerPool retry policy on TransientError + fatal exception."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from matrix.model.except_ import TransientError
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


class _RaisingExecutor:
    def __init__(self, exc):
        self._exc = exc

    async def invoke(self, _messages):
        raise self._exc


class _NoopPersist:
    async def persist_turn(self, turn_no):
        return None


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


async def _setup(scheduler, sid, monkeypatch, exc, *,
                 max_attempts=5, attempt_count=0):
    scheduler.register_session_for_test(sid)
    scheduler._sessions[sid].attempt_count = attempt_count
    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=1, max_attempts=max_attempts,
            base_backoff_seconds=1.0, max_backoff_seconds=10.0,
        ),
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
        attempt_count=attempt_count,
    )
    monkeypatch.setattr(pool, "_load_session",
                        lambda _sid: _async_return(fake_session))
    monkeypatch.setattr(pool, "_load_workspace_for_persist",
                        lambda _ws: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_executor",
                        lambda _s, _w: _async_return(_RaisingExecutor(exc)))
    return pool, lease


async def test_transient_error_re_enqueues_and_records_failure(
    scheduler, monkeypatch,
):
    pool, lease = await _setup(
        scheduler, "sess-tr-1", monkeypatch,
        TransientError("net blip"), attempt_count=0,
    )
    await pool._run_one_turn(lease)
    snapshot = scheduler.session_snapshot_for_test("sess-tr-1")
    assert snapshot.attempt_count == 1
    assert snapshot.last_error == "net blip"
    assert scheduler._leases["sess-tr-1"].runnable is True


async def test_transient_exhausted_ends_failed(scheduler, monkeypatch):
    pool, lease = await _setup(
        scheduler, "sess-exh-1", monkeypatch,
        TransientError("kept failing"),
        max_attempts=2, attempt_count=1,
    )
    await pool._run_one_turn(lease)
    snapshot = scheduler.session_snapshot_for_test("sess-exh-1")
    assert snapshot.status == SessionStatus.ENDED


async def test_fatal_error_ends_failed(scheduler, monkeypatch):
    pool, lease = await _setup(
        scheduler, "sess-fa-1", monkeypatch, ValueError("boom"),
    )
    await pool._run_one_turn(lease)
    snapshot = scheduler.session_snapshot_for_test("sess-fa-1")
    assert snapshot.status == SessionStatus.ENDED
