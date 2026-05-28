"""Tests for EventDrivenWatcher + WatcherManager.

Two layers:

* :class:`EventDrivenWatcher` — the unit.  Consumes push events from a
  :class:`~matrix.bus.ws_watch_probe.WatchProbe` and fires a callback when
  changes land, coalescing bursts via ``batch_window_ms``.
* :class:`WatcherManager` — the lifecycle owner.  Periodically scans the
  scheduler for ``watch:*`` parks and starts / stops watchers to match.
  Publishes change bursts on the event bus on behalf of each watcher.

The unit tests use a fake :class:`WatchProbe` that yields injected
:class:`~matrix.bus.ws_watch_probe.Change` events.  The integration tests
wire a real :class:`~matrix.bus.host_inotify_probe.HostInotifyProbe` against
real on-disk files so that the full push path executes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.bus.host_inotify_probe import HostInotifyProbe
from matrix.bus.watcher import (
    EventDrivenWatcher,
    WatcherManager,
)
from matrix.bus.ws_watch_probe import Change, WatchProbe
from matrix.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState


# ===========================================================================
# Fake WatchProbe for unit tests
# ===========================================================================


class _FakeWatchProbe(WatchProbe):
    """A WatchProbe that yields pre-injected Change events.

    Call :meth:`inject` to queue events; the :meth:`watch` iterator yields
    them and then blocks until more are injected or the caller closes it.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Change | None] = asyncio.Queue()

    def inject(self, change: Change) -> None:
        self._queue.put_nowait(change)

    def close(self) -> None:
        """Signal the iterator to terminate."""
        self._queue.put_nowait(None)

    async def watch(self, paths: list[str]) -> AsyncIterator[Change]:  # type: ignore[override]
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


# ===========================================================================
# EventDrivenWatcher (unit)
# ===========================================================================


@pytest.mark.asyncio
class TestEventDrivenWatcher:
    async def test_emits_modified_event(self):
        probe = _FakeWatchProbe()
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        w = EventDrivenWatcher(
            probe=probe,
            paths=["a.txt"],
            batch_window_ms=20,
            on_change=on_change,
        )
        w.start()
        try:
            probe.inject(Change(path="a.txt", event_type="modify", mtime=1716000000.0))
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
            probe.close()
            await w.stop()

    async def test_emits_created_event(self):
        probe = _FakeWatchProbe()
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        w = EventDrivenWatcher(
            probe=probe,
            paths=["new.txt"],
            batch_window_ms=20,
            on_change=on_change,
        )
        w.start()
        try:
            probe.inject(Change(path="new.txt", event_type="create"))
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
            probe.close()
            await w.stop()

    async def test_emits_deleted_event(self):
        probe = _FakeWatchProbe()
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        w = EventDrivenWatcher(
            probe=probe,
            paths=["doomed.txt"],
            batch_window_ms=20,
            on_change=on_change,
        )
        w.start()
        try:
            probe.inject(Change(path="doomed.txt", event_type="delete"))
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
            probe.close()
            await w.stop()

    async def test_coalesces_burst_within_batch_window(self):
        probe = _FakeWatchProbe()
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        w = EventDrivenWatcher(
            probe=probe,
            paths=["a", "b"],
            batch_window_ms=150,
            on_change=on_change,
        )
        w.start()
        try:
            probe.inject(Change(path="a", event_type="modify"))
            await asyncio.sleep(0.02)
            probe.inject(Change(path="b", event_type="modify"))
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received
            burst = received[-1]
            paths = {c["path"] for c in burst}
            assert paths == {"a", "b"}
        finally:
            probe.close()
            await w.stop()

    async def test_last_writer_wins_within_batch_window(self):
        """If the same path changes twice within the batch window, only the
        last event is emitted."""
        probe = _FakeWatchProbe()
        received: list[list[dict]] = []

        async def on_change(changes):
            received.append(changes)

        w = EventDrivenWatcher(
            probe=probe,
            paths=["x.txt"],
            batch_window_ms=150,
            on_change=on_change,
        )
        w.start()
        try:
            probe.inject(Change(path="x.txt", event_type="create"))
            await asyncio.sleep(0.02)
            probe.inject(Change(path="x.txt", event_type="modify", mtime=9999.0))
            for _ in range(50):
                await asyncio.sleep(0.02)
                if received:
                    break
            assert received
            burst = received[-1]
            # Only one change for x.txt; the later "modify" overwrites "create".
            x_changes = [c for c in burst if c["path"] == "x.txt"]
            assert len(x_changes) == 1
            assert x_changes[0]["event_type"] == "modified"
        finally:
            probe.close()
            await w.stop()

    async def test_stop_is_idempotent(self):
        probe = _FakeWatchProbe()
        w = EventDrivenWatcher(
            probe=probe,
            paths=["a"],
            batch_window_ms=10,
            on_change=lambda c: None,
        )
        w.start()
        probe.close()
        await w.stop()
        await w.stop()  # second call must not raise


# ===========================================================================
# EventDrivenWatcher with real HostInotifyProbe (inotify integration)
# ===========================================================================


@pytest.mark.asyncio
async def test_event_driven_watcher_with_host_inotify_probe(tmp_path):
    """EventDrivenWatcher + HostInotifyProbe fires on a real file mutation."""
    target = tmp_path / "f.txt"
    target.write_text("v1")

    received: list[list[dict]] = []

    async def on_change(changes):
        received.append(changes)

    probe = HostInotifyProbe(root=str(tmp_path))
    w = EventDrivenWatcher(
        probe=probe,
        paths=["f.txt"],
        batch_window_ms=20,
        on_change=on_change,
    )
    w.start()
    try:
        await asyncio.sleep(0.1)  # give watchfiles time to register
        target.write_text("v2")
        for _ in range(50):
            await asyncio.sleep(0.05)
            if received:
                break
        assert received, "watcher never fired"
        burst = received[-1]
        assert any(c["path"] == "f.txt" for c in burst)
    finally:
        await w.stop()


# ===========================================================================
# WatcherManager (integration with bus + scheduler)
# ===========================================================================


def _make_watch_parked_session(
    *,
    session_id: str,
    tool_call_id: str,
    workspace_id: str,
    paths: list[str],
) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
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

    Holds a fixed mapping ``workspace_id → WatchProbe``.
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

        probe = HostInotifyProbe(root=str(tmp_path))
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
            # Give the manager a tick to start the watcher + watchfiles to register.
            await asyncio.sleep(0.3)
            target.write_text("v2")
            # The watcher should publish a burst on the bus.
            event = await asyncio.wait_for(anext(sub), timeout=3.0)
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
        probe = HostInotifyProbe(root=str(tmp_path))
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
        sess = WorkspaceSession(
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
        probe = HostInotifyProbe(root=str(tmp_path))
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
