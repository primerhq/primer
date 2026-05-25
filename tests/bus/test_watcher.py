"""Tests for WorkspaceFilesWatcher + WatcherManager.

Two layers:

* :class:`WorkspaceFilesWatcher` — the unit. Polls via a StatProbe for
  a list of paths, fires a callback when changes land, coalesces bursts
  with ``batch_window_ms``.
* :class:`WatcherManager` — the lifecycle owner. Periodically scans
  the scheduler for ``watch:*`` parks and starts / stops watchers to
  match. Publishes change bursts on the event bus on behalf of each
  watcher.

The tests use real on-disk files (under ``tmp_path``) via
:class:`HostStatProbe` because that's the path the production watcher
exercises for local workspaces. They use very short poll windows so each
test runs in milliseconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.bus.watcher import (
    HostStatProbe,
    WatcherManager,
    WorkspaceFilesWatcher,
)
from matrix.model.session import (
    AgentSessionBinding,
    Session,
    SessionStatus,
)
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState


# ===========================================================================
# WorkspaceFilesWatcher (unit)
# ===========================================================================


@pytest.mark.asyncio
class TestWorkspaceFilesWatcher:
    async def test_emits_modified_event_on_mtime_change(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1")
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        probe = HostStatProbe(root=tmp_path)
        w = WorkspaceFilesWatcher(
            probe=probe,
            paths=["a.txt"],
            batch_window_ms=20,
            poll_interval_seconds=0.02,
            on_change=on_change,
        )
        w.start()
        try:
            # Bump mtime past the watcher's first stat baseline.
            await asyncio.sleep(0.05)
            f.write_text("v2")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received, "watcher never fired"
            burst = received[-1]
            assert any(
                c["path"] == "a.txt" and c["event_type"] == "modified"
                for c in burst
            )
        finally:
            await w.stop()

    async def test_emits_created_event_when_missing_file_appears(
        self, tmp_path,
    ):
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        probe = HostStatProbe(root=tmp_path)
        w = WorkspaceFilesWatcher(
            probe=probe,
            paths=["new.txt"],
            batch_window_ms=20,
            poll_interval_seconds=0.02,
            on_change=on_change,
        )
        w.start()
        try:
            await asyncio.sleep(0.05)
            (tmp_path / "new.txt").write_text("hello")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received
            assert any(
                c["path"] == "new.txt" and c["event_type"] == "created"
                for c in received[-1]
            )
        finally:
            await w.stop()

    async def test_emits_deleted_event_when_file_removed(self, tmp_path):
        f = tmp_path / "doomed.txt"
        f.write_text("x")
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        probe = HostStatProbe(root=tmp_path)
        w = WorkspaceFilesWatcher(
            probe=probe,
            paths=["doomed.txt"],
            batch_window_ms=20,
            poll_interval_seconds=0.02,
            on_change=on_change,
        )
        w.start()
        try:
            await asyncio.sleep(0.05)
            f.unlink()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received
            assert any(
                c["path"] == "doomed.txt" and c["event_type"] == "deleted"
                for c in received[-1]
            )
        finally:
            await w.stop()

    async def test_coalesces_burst_within_batch_window(self, tmp_path):
        f1 = tmp_path / "a"
        f2 = tmp_path / "b"
        f1.write_text("1")
        f2.write_text("1")
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        probe = HostStatProbe(root=tmp_path)
        w = WorkspaceFilesWatcher(
            probe=probe,
            paths=["a", "b"],
            batch_window_ms=150,  # generous window — both writes fall in
            poll_interval_seconds=0.02,
            on_change=on_change,
        )
        w.start()
        try:
            await asyncio.sleep(0.05)
            f1.write_text("2")
            await asyncio.sleep(0.02)
            f2.write_text("2")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received
            burst = received[-1]
            paths = {c["path"] for c in burst}
            assert paths == {"a", "b"}
        finally:
            await w.stop()

    async def test_stop_is_idempotent(self, tmp_path):
        probe = HostStatProbe(root=tmp_path)
        w = WorkspaceFilesWatcher(
            probe=probe,
            paths=["a"],
            batch_window_ms=10,
            poll_interval_seconds=0.02,
            on_change=lambda c: None,  # ignored — never fires
        )
        w.start()
        await w.stop()
        await w.stop()  # second call must not raise


# ===========================================================================
# WatcherManager (integration with bus + scheduler)
# ===========================================================================


def _make_watch_parked_session(
    *,
    session_id: str,
    tool_call_id: str,
    workspace_id: str,
    paths: list[str],
) -> Session:
    now = datetime.now(timezone.utc)
    sess = Session(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    event_key = f"watch:{session_id}:{tool_call_id}"
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "watch_files",
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": {
                "paths": paths,
                "batch_window_ms": 30,
                "workspace_id": workspace_id,
                "tool_call_id": tool_call_id,
                "registered_at_iso": now.isoformat(),
            },
        },
        "llm_messages": [],
        "turn_no": 1,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    return sess


class _StaticProbeResolver:
    """Test stand-in for the production probe resolver.

    Holds a fixed mapping ``workspace_id → StatProbe``.
    """

    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    async def resolve(self, workspace_id: str) -> object:
        return self._mapping.get(workspace_id)


@pytest.mark.asyncio
class TestWatcherManager:
    async def test_manager_starts_watcher_for_parked_session_and_publishes_on_change(
        self, tmp_path,
    ):
        # Wire bus + scheduler + manager.
        bus = InMemoryEventBus()
        await bus.initialize()
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.register_worker(
            worker_id="wrk-1", host="h", pid=1, capacity=1,
        )

        target = tmp_path / "f.txt"
        target.write_text("v1")

        sess = _make_watch_parked_session(
            session_id="sess-A",
            tool_call_id="tc-A",
            workspace_id="ws-A",
            paths=["f.txt"],
        )
        scheduler._sessions["sess-A"] = sess
        scheduler._leases["sess-A"] = _LeaseState(
            worker_id=None,
            expires_at=None,
            runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        probe = HostStatProbe(root=tmp_path)
        resolver = _StaticProbeResolver({"ws-A": probe})
        mgr = WatcherManager(
            bus=bus,
            scheduler=scheduler,
            workspace_root_resolver=resolver.resolve,
            scan_interval_seconds=0.05,
            poll_interval_seconds=0.02,
        )

        # Subscribe FIRST so the published event lands on our queue.
        sub = bus.subscribe()
        mgr.start()
        try:
            # Give the manager a tick to start the watcher.
            await asyncio.sleep(0.15)
            target.write_text("v2")
            # The watcher should publish a burst on the bus.
            event = await asyncio.wait_for(anext(sub), timeout=2.0)
            assert event.event_key == "watch:sess-A:tc-A"
            assert "changes" in event.payload
            paths = [c["path"] for c in event.payload["changes"]]
            assert "f.txt" in paths
        finally:
            await sub.aclose()
            await mgr.stop()
            await scheduler.aclose()
            await bus.aclose()

    async def test_manager_stops_watcher_when_park_flips_to_resumable(
        self, tmp_path,
    ):
        bus = InMemoryEventBus()
        await bus.initialize()
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.register_worker(
            worker_id="wrk-1", host="h", pid=1, capacity=1,
        )
        (tmp_path / "x").write_text("1")
        sess = _make_watch_parked_session(
            session_id="sess-B",
            tool_call_id="tc-B",
            workspace_id="ws-B",
            paths=["x"],
        )
        scheduler._sessions["sess-B"] = sess
        scheduler._leases["sess-B"] = _LeaseState(
            worker_id=None,
            expires_at=None,
            runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )
        probe = HostStatProbe(root=tmp_path)
        resolver = _StaticProbeResolver({"ws-B": probe})
        mgr = WatcherManager(
            bus=bus,
            scheduler=scheduler,
            workspace_root_resolver=resolver.resolve,
            scan_interval_seconds=0.05,
            poll_interval_seconds=0.02,
        )
        mgr.start()
        try:
            await asyncio.sleep(0.15)
            assert "watch:sess-B:tc-B" in mgr.active_watchers()
            # Simulate the listener flipping the row after a different
            # publisher (e.g. cancel-yielded-tool) hit the bus.
            sess.parked_status = "resumable"
            # Wait for the manager's next scan to drop the watcher.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if "watch:sess-B:tc-B" not in mgr.active_watchers():
                    break
            assert "watch:sess-B:tc-B" not in mgr.active_watchers()
        finally:
            await mgr.stop()
            await scheduler.aclose()
            await bus.aclose()

    async def test_manager_ignores_non_watch_parks(self, tmp_path):
        bus = InMemoryEventBus()
        await bus.initialize()
        scheduler = InMemoryScheduler()
        await scheduler.initialize()
        await scheduler.register_worker(
            worker_id="wrk-1", host="h", pid=1, capacity=1,
        )
        # An ask_user park — must NOT spawn a file watcher.
        sess = Session(
            id="sess-C",
            workspace_id="ws-C",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "ask_user:sess-C:tc-C"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "tool_call_id": "tc-C",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": "ask_user:sess-C:tc-C",
                "timeout": 30.0,
                "resume_metadata": {"prompt": "?"},
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": None,
        }
        scheduler._sessions["sess-C"] = sess
        scheduler._leases["sess-C"] = _LeaseState(
            worker_id=None,
            expires_at=None,
            runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )
        probe = HostStatProbe(root=tmp_path)
        resolver = _StaticProbeResolver({"ws-C": probe})
        mgr = WatcherManager(
            bus=bus,
            scheduler=scheduler,
            workspace_root_resolver=resolver.resolve,
            scan_interval_seconds=0.05,
            poll_interval_seconds=0.02,
        )
        mgr.start()
        try:
            await asyncio.sleep(0.2)
            assert mgr.active_watchers() == set()
        finally:
            await mgr.stop()
            await scheduler.aclose()
            await bus.aclose()
