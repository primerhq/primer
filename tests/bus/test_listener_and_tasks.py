"""Integration tests for the listener + timer + sweeper background tasks.

Exercises the M2 wake path end-to-end:

1. A session is parked (writes parked_status='parked' + parked_event_key).
2. The listener is running.
3. We publish an event to the bus.
4. Listener observes → scheduler.mark_resumable(event_key, payload).
5. Parked session flips to 'resumable'; lease re-armed.

Also tests:
* TimerScheduler — finds due timer:* parks, publishes events.
* TimeoutSweeper — finds expired non-timer parks, publishes timeout markers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from matrix.bus.in_memory import InMemoryEventBus
from matrix.bus.listener import YieldEventListener
from matrix.bus.scheduler_tasks import TimeoutSweeper, TimerScheduler
from matrix.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState


def _make_parked_session(
    *,
    session_id: str,
    event_key: str,
    parked_until: datetime,
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_until = parked_until
    sess.parked_at = datetime.now(timezone.utc)
    sess.parked_state = {
        "schema_version": 1,
        "yielded": {
            "tool_name": "sleep",
            "event_key": event_key,
            "timeout": 30.0,
            "resume_metadata": {},
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": sess.parked_at.isoformat(),
        "resume_event_payload": None,
    }
    return sess


@pytest.fixture
async def harness():
    """Bus + scheduler + listener + worker, all wired up."""
    bus = InMemoryEventBus()
    await bus.initialize()
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    await scheduler.register_worker(
        worker_id="wrk-1", host="h", pid=1, capacity=1,
    )
    listener = YieldEventListener(bus=bus, scheduler=scheduler)
    listener.start()
    try:
        yield bus, scheduler, listener
    finally:
        await listener.stop()
        await scheduler.aclose()
        await bus.aclose()


@pytest.mark.asyncio
class TestYieldEventListener:
    async def test_listener_flips_parked_to_resumable(self, harness):
        bus, scheduler, _listener = harness
        sess = _make_parked_session(
            session_id="sess-A",
            event_key="timer:tc-A",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        scheduler._sessions["sess-A"] = sess
        scheduler._leases["sess-A"] = _LeaseState(
            worker_id=None,
            expires_at=None,
            runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        await bus.publish("timer:tc-A", {})
        # Give the listener time to consume + flip.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_status == "resumable"
        assert scheduler._leases["sess-A"].runnable is True

    async def test_listener_ignores_unmatched_event_keys(self, harness):
        bus, scheduler, _listener = harness
        sess = _make_parked_session(
            session_id="sess-B",
            event_key="timer:tc-B",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        scheduler._sessions["sess-B"] = sess
        scheduler._leases["sess-B"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        # Publish an event with a different key — should not flip.
        await bus.publish("timer:something-else", {})
        await asyncio.sleep(0.1)
        assert sess.parked_status == "parked"

    async def test_listener_double_publish_only_first_wins(self, harness):
        bus, scheduler, _listener = harness
        sess = _make_parked_session(
            session_id="sess-C",
            event_key="timer:tc-C",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        scheduler._sessions["sess-C"] = sess
        scheduler._leases["sess-C"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        await bus.publish("timer:tc-C", {"winner": "first"})
        await bus.publish("timer:tc-C", {"winner": "second"})
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        # First payload wins.
        assert sess.parked_state["resume_event_payload"] == {"winner": "first"}


@pytest.mark.asyncio
class TestTimerScheduler:
    async def test_due_timer_park_gets_published(self, harness):
        bus, scheduler, _listener = harness
        # Park whose deadline is in the past — due.
        sess = _make_parked_session(
            session_id="sess-T",
            event_key="timer:tc-T",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        scheduler._sessions["sess-T"] = sess
        scheduler._leases["sess-T"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        timer = TimerScheduler(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        timer.start()
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if sess.parked_status == "resumable":
                    break
            assert sess.parked_status == "resumable"
        finally:
            await timer.stop()

    async def test_not_yet_due_timer_park_not_published(self, harness):
        bus, scheduler, _listener = harness
        # Park whose deadline is in the future — not due.
        sess = _make_parked_session(
            session_id="sess-U",
            event_key="timer:tc-U",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        scheduler._sessions["sess-U"] = sess
        scheduler._leases["sess-U"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        timer = TimerScheduler(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        timer.start()
        try:
            # Give the timer multiple ticks to potentially misfire.
            await asyncio.sleep(0.3)
            assert sess.parked_status == "parked"
        finally:
            await timer.stop()


@pytest.mark.asyncio
class TestTimeoutSweeper:
    async def test_sweeper_publishes_timeout_marker_for_expired_park(
        self, harness,
    ):
        bus, scheduler, _listener = harness
        # ask_user park (non-timer) whose deadline elapsed.
        sess = _make_parked_session(
            session_id="sess-S",
            event_key="ask_user:sess-S:tc-S",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        scheduler._sessions["sess-S"] = sess
        scheduler._leases["sess-S"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        sweeper = TimeoutSweeper(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        sweeper.start()
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if sess.parked_status == "resumable":
                    break
            assert sess.parked_status == "resumable"
            # Payload carries the timeout marker — resume hook will
            # convert it to YieldTimeout via classify_resume_payload.
            payload = sess.parked_state["resume_event_payload"]
            assert payload.get("__yield_timeout__") is True
        finally:
            await sweeper.stop()

    async def test_sweeper_does_not_publish_for_timer_parks(self, harness):
        # Timer parks are the TimerScheduler's responsibility, not
        # the sweeper's. The sweeper only handles non-timer:* keys
        # so it doesn't double-publish.
        bus, scheduler, _listener = harness
        sess = _make_parked_session(
            session_id="sess-skip",
            event_key="timer:tc-skip",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        scheduler._sessions["sess-skip"] = sess
        scheduler._leases["sess-skip"] = _LeaseState(
            worker_id=None, expires_at=None, runnable=False,
            next_attempt_at=datetime.now(timezone.utc),
        )

        sweeper = TimeoutSweeper(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        sweeper.start()
        try:
            await asyncio.sleep(0.2)
            # Sweeper ignored it (only the TimerScheduler would
            # have flipped it).
            assert sess.parked_status == "parked"
        finally:
            await sweeper.stop()
