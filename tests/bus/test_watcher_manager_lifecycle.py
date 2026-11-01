"""WatcherManager lifecycle: dead-watcher restart and teardown on lease loss.

CORE-6: (a) a watcher whose probe stream dies (a runtime websocket drop)
ends its task, the key stays in ``_watchers``, and a plain membership
check never restarts it, so the park only times out; (b) losing the
leader lease cancels the manager's ``_run`` without stopping the live
watchers, which then keep running on the old leader (leaking inotify
instances and duplicating wakes) while the new leader starts its own.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.watcher import WatcherManager
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler, _LeaseState
from tests.conftest import _FakeStorageProvider

EVENT_KEY = "watch:s1:tc1"


class _NoBus:
    async def publish(self, *a, **k):
        pass


def _parked_session() -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
        id="s1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(kind="agent", agent_id="a"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    sess.parked_status = "parked"
    sess.parked_event_key = EVENT_KEY
    sess.parked_until = now + timedelta(minutes=10)
    sess.parked_state = {
        "yielded": {
            "resume_metadata": {
                "paths": ["a.txt"],
                "batch_window_ms": 10,
            }
        }
    }
    return sess


async def _manager_with(probe) -> WatcherManager:
    sp = _FakeStorageProvider()
    sched = InMemoryScheduler(storage_provider=sp)
    await sched.initialize()
    await sched.register_worker(
        worker_id="wrk-1", host="h", pid=1, capacity=1,
    )
    await sp.get_storage(WorkspaceSession).create(_parked_session())
    sched._leases["s1"] = _LeaseState(
        worker_id=None,
        expires_at=None,
        runnable=False,
        next_attempt_at=datetime.now(timezone.utc),
    )

    async def resolve(_workspace_id: str):
        return probe

    return WatcherManager(
        bus=_NoBus(),
        scheduler=sched,
        workspace_root_resolver=resolve,
        scan_interval_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_a_dead_watcher_is_restarted_on_the_next_scan():
    """A probe whose stream dies ends the watcher task; the next scans
    must start a fresh watcher (the inverted review probe: it asserted
    the stream was opened exactly once over three scans)."""

    class _DeadProbe:
        def __init__(self) -> None:
            self.calls = 0

        async def watch(self, _paths):
            self.calls += 1
            raise ConnectionError("runtime websocket dropped")
            yield  # pragma: no cover

    probe = _DeadProbe()
    mgr = await _manager_with(probe)
    try:
        await mgr._scan_once()
        await asyncio.sleep(0.05)  # the doomed task raises and ends
        assert probe.calls == 1

        await mgr._scan_once()
        await asyncio.sleep(0.05)
        await mgr._scan_once()
        await asyncio.sleep(0.05)
        # today the key stays in _watchers and no scan restarts it
        assert probe.calls == 3
    finally:
        await mgr.stop()


class _EndlessProbe:
    """A probe whose stream never yields and never ends: it stands in
    for a live runtime websocket."""

    def __init__(self) -> None:
        self.stopped = False

    async def watch(self, _paths):
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        yield  # pragma: no cover


class _Lease:
    def __init__(self) -> None:
        self.lost_event = asyncio.Event()
        self.releases = 0

    async def release(self) -> None:
        self.releases += 1


class _OneShotElector:
    """Hands the lease out once; after the loss the role belongs to
    someone else, so re-acquire answers None."""

    def __init__(self) -> None:
        self.lease = _Lease()
        self.acquires = 0

    async def try_acquire(self, _role):
        self.acquires += 1
        if self.acquires == 1:
            return self.lease
        return None


@pytest.mark.asyncio
async def test_losing_the_lease_stops_every_live_watcher():
    """The supervisor cancels _run on lease loss; the live watchers must
    stop with it, or they keep running on the old leader (CORE-6)."""
    probe = _EndlessProbe()
    mgr = await _manager_with(probe)
    elector = _OneShotElector()
    mgr.start(elector)
    try:
        for _ in range(300):
            if mgr.active_watchers():
                break
            await asyncio.sleep(0.01)
        assert mgr.active_watchers() == {EVENT_KEY}

        elector.lease.lost_event.set()
        for _ in range(500):
            if not mgr.active_watchers():
                break
            await asyncio.sleep(0.01)
        # today the cancelled _run never stops the watcher
        assert mgr.active_watchers() == set()
        assert probe.stopped
        assert elector.lease.releases == 1
    finally:
        await mgr.stop()
