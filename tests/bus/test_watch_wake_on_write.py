"""Tests for deterministic ``watch_files`` wake-on-write.

Covers :func:`primer.bus.watch_notify.wake_watch_files_on_write`, which lets
the REST file-write path explicitly wake a ``watch_files``-parked session in
the same workspace whose watched paths match the written file — reusing the
inotify watcher's change-payload shape and the existing event-bus resume
path.

The tests use a real :class:`~primer.scheduler.in_memory.InMemoryScheduler`
(so the same ``_find_active_watch_parks`` query the WatcherManager uses is
exercised) plus a spy event bus that records ``publish`` calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.watch_notify import (
    path_matches_watch,
    wake_watch_files_on_write,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler, _LeaseState


# ===========================================================================
# Spy event bus
# ===========================================================================


class _SpyBus:
    """Records every ``publish(event_key, payload)`` call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.raise_on_publish = False

    async def publish(self, event_key, payload=None):
        if self.raise_on_publish:
            raise RuntimeError("bus exploded")
        self.published.append((event_key, dict(payload or {})))


# ===========================================================================
# Park construction helper
# ===========================================================================


def _make_watch_parked_session(
    *,
    session_id: str,
    tool_call_id: str,
    workspace_id: str,
    paths: list[str],
    event_key: str | None = None,
    parked_status: str = "parked",
) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    sess = WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    key = event_key or f"watch:{session_id}:{tool_call_id}"
    sess.parked_status = parked_status  # type: ignore[assignment]
    sess.parked_event_key = key
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "watch_files",
            "event_key": key,
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


async def _scheduler_with(*sessions: WorkspaceSession) -> InMemoryScheduler:
    sched = InMemoryScheduler()
    await sched.initialize()
    for sess in sessions:
        sched._sessions[sess.id] = sess
        sched._leases[sess.id] = _LeaseState(
            worker_id=None,
            expires_at=None,
            runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )
    return sched


# ===========================================================================
# path_matches_watch
# ===========================================================================


class TestPathMatchesWatch:
    def test_exact_file(self):
        assert path_matches_watch("src/app.py", "src/app.py")

    def test_glob(self):
        assert path_matches_watch("src/app.py", "src/*.py")
        assert not path_matches_watch("src/sub/app.py", "src/*.py")

    def test_directory_watch_matches_nested(self):
        assert path_matches_watch("src/sub/app.py", "src")
        assert path_matches_watch("src/app.py", "src")

    def test_non_match(self):
        assert not path_matches_watch("docs/readme.md", "src/*.py")
        assert not path_matches_watch("docs/readme.md", "src")

    def test_leading_dot_slash_normalised(self):
        assert path_matches_watch("./src/app.py", "src/*.py")


# ===========================================================================
# wake_watch_files_on_write
# ===========================================================================


@pytest.mark.asyncio
class TestWakeOnWrite:
    async def test_matching_path_publishes_change_payload(self):
        sess = _make_watch_parked_session(
            session_id="s1",
            tool_call_id="tc1",
            workspace_id="W",
            paths=["src/*.py"],
        )
        sched = await _scheduler_with(sess)
        bus = _SpyBus()
        try:
            woken = await wake_watch_files_on_write(
                workspace_id="W",
                path="src/app.py",
                scheduler=sched,
                event_bus=bus,
            )
        finally:
            await sched.aclose()

        assert woken == 1
        assert len(bus.published) == 1
        event_key, payload = bus.published[0]
        assert event_key == "watch:s1:tc1"
        # Same shape the inotify watcher publishes.
        assert "changes" in payload
        assert isinstance(payload["changes"], list)
        change = payload["changes"][0]
        assert change["path"] == "src/app.py"
        assert change["event_type"] == "modified"
        assert "mtime_after" in change

    async def test_non_matching_path_no_publish(self):
        sess = _make_watch_parked_session(
            session_id="s1",
            tool_call_id="tc1",
            workspace_id="W",
            paths=["src/*.py"],
        )
        sched = await _scheduler_with(sess)
        bus = _SpyBus()
        try:
            woken = await wake_watch_files_on_write(
                workspace_id="W",
                path="docs/readme.md",
                scheduler=sched,
                event_bus=bus,
            )
        finally:
            await sched.aclose()
        assert woken == 0
        assert bus.published == []

    async def test_different_workspace_no_publish(self):
        sess = _make_watch_parked_session(
            session_id="s1",
            tool_call_id="tc1",
            workspace_id="W",
            paths=["src/*.py"],
        )
        sched = await _scheduler_with(sess)
        bus = _SpyBus()
        try:
            woken = await wake_watch_files_on_write(
                workspace_id="OTHER",
                path="src/app.py",
                scheduler=sched,
                event_bus=bus,
            )
        finally:
            await sched.aclose()
        assert woken == 0
        assert bus.published == []

    async def test_non_watch_park_ignored(self):
        # An ask_user park in the same workspace must not be woken by a
        # file write — its event_key does not start with "watch:".
        sess = _make_watch_parked_session(
            session_id="s2",
            tool_call_id="tc2",
            workspace_id="W",
            paths=["src/*.py"],
            event_key="ask_user:s2:tc2",
        )
        sched = await _scheduler_with(sess)
        bus = _SpyBus()
        try:
            woken = await wake_watch_files_on_write(
                workspace_id="W",
                path="src/app.py",
                scheduler=sched,
                event_bus=bus,
            )
        finally:
            await sched.aclose()
        assert woken == 0
        assert bus.published == []

    async def test_resumable_park_not_rewoken(self):
        # _find_active_watch_parks only returns rows still in 'parked'.
        sess = _make_watch_parked_session(
            session_id="s1",
            tool_call_id="tc1",
            workspace_id="W",
            paths=["src/*.py"],
            parked_status="resumable",
        )
        sched = await _scheduler_with(sess)
        bus = _SpyBus()
        try:
            woken = await wake_watch_files_on_write(
                workspace_id="W",
                path="src/app.py",
                scheduler=sched,
                event_bus=bus,
            )
        finally:
            await sched.aclose()
        assert woken == 0
        assert bus.published == []
