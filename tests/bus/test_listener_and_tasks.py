"""Integration tests for the listener + timer + sweeper background tasks.

Exercises the wake path end-to-end:

1. A session is parked (writes parked_status='parked' + parked_event_key).
2. The listener is running (wired to session storage + engine).
3. We publish an event to the bus.
4. Listener observes -> finds parked sessions via storage.find, flips
   parked -> resumable, re-arms the engine lease.
5. Parked session is resumable; lease is claimable.

Also tests:
* TimerScheduler -- finds due timer:* parks, publishes events.
* TimeoutSweeper -- finds expired non-timer parks, publishes timeout markers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.bus.listener import YieldEventListener
from primer.bus.scheduler_tasks import TimeoutSweeper, TimerScheduler
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from primer.scheduler.in_memory import InMemoryScheduler, _LeaseState
from primer.storage.sqlite import SqliteStorageProvider


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
async def harness(tmp_path: Path):
    """Bus + scheduler + storage + engine + listener, all wired up.

    * scheduler -- still used by TimerScheduler / TimeoutSweeper to
      find which event_keys are due (they walk scheduler._sessions).
    * storage / engine -- used by the new YieldEventListener to find
      parked sessions via storage.find and re-arm leases via
      engine.mark_resumable.
    """
    bus = InMemoryEventBus()
    await bus.initialize()

    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    await scheduler.register_worker(
        worker_id="wrk-1", host="h", pid=1, capacity=1,
    )

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "test.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=storage)},
    )

    listener = YieldEventListener(bus=bus, session_storage=storage, engine=engine)
    listener.start()
    try:
        yield bus, scheduler, storage, engine, listener
    finally:
        await listener.stop()
        await scheduler.aclose()
        await provider.aclose()
        await bus.aclose()


async def _seed_parked(storage, scheduler, sess: WorkspaceSession) -> None:
    """Seed a parked session into both storage AND the scheduler's dict.

    Storage is what the new listener queries; scheduler._sessions is
    what TimerScheduler / TimeoutSweeper walk to find due event_keys.
    """
    await storage.create(sess)
    scheduler._sessions[sess.id] = sess
    scheduler._leases[sess.id] = _LeaseState(
        worker_id=None,
        expires_at=None,
        runnable=False,
        next_attempt_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
class TestYieldEventListener:
    async def test_listener_flips_parked_to_resumable(self, harness):
        bus, scheduler, storage, engine, _listener = harness
        sess = _make_parked_session(
            session_id="sess-A",
            event_key="timer:tc-A",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        await _seed_parked(storage, scheduler, sess)

        await bus.publish("timer:tc-A", {})
        # Give the listener time to consume + flip.
        row = None
        for _ in range(50):
            await asyncio.sleep(0.02)
            row = await storage.get("sess-A")
            if row is not None and row.parked_status == "resumable":
                break
        assert row is not None
        assert row.parked_status == "resumable"
        # Lease re-armed: engine should return this session as claimable.
        leases = await engine.claim_due("wrk-1", max_count=10)
        assert "sess-A" in [le.entity_id for le in leases]

    async def test_listener_ignores_unmatched_event_keys(self, harness):
        bus, scheduler, storage, engine, _listener = harness
        sess = _make_parked_session(
            session_id="sess-B",
            event_key="timer:tc-B",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        await _seed_parked(storage, scheduler, sess)

        # Publish an event with a different key -- should not flip.
        await bus.publish("timer:something-else", {})
        await asyncio.sleep(0.1)
        row = await storage.get("sess-B")
        assert row is not None
        assert row.parked_status == "parked"

    async def test_listener_double_publish_only_first_wins(self, harness):
        bus, scheduler, storage, engine, _listener = harness
        sess = _make_parked_session(
            session_id="sess-C",
            event_key="timer:tc-C",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        await _seed_parked(storage, scheduler, sess)

        await bus.publish("timer:tc-C", {"winner": "first"})
        await bus.publish("timer:tc-C", {"winner": "second"})
        row = None
        for _ in range(50):
            await asyncio.sleep(0.02)
            row = await storage.get("sess-C")
            if row is not None and row.parked_status == "resumable":
                break
        assert row is not None
        # First payload wins.
        assert row.parked_state is not None
        assert row.parked_state["resume_event_payload"] == {"winner": "first"}


@pytest.mark.asyncio
class TestTimerScheduler:
    async def test_due_timer_park_gets_published(self, harness):
        bus, scheduler, storage, engine, _listener = harness
        # Park whose deadline is in the past -- due.
        sess = _make_parked_session(
            session_id="sess-T",
            event_key="timer:tc-T",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await _seed_parked(storage, scheduler, sess)

        timer = TimerScheduler(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        timer.start()
        try:
            row = None
            for _ in range(50):
                await asyncio.sleep(0.02)
                row = await storage.get("sess-T")
                if row is not None and row.parked_status == "resumable":
                    break
            assert row is not None
            assert row.parked_status == "resumable"
        finally:
            await timer.stop()

    async def test_not_yet_due_timer_park_not_published(self, harness):
        bus, scheduler, storage, engine, _listener = harness
        # Park whose deadline is in the future -- not due.
        sess = _make_parked_session(
            session_id="sess-U",
            event_key="timer:tc-U",
            parked_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        await _seed_parked(storage, scheduler, sess)

        timer = TimerScheduler(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        timer.start()
        try:
            # Give the timer multiple ticks to potentially misfire.
            await asyncio.sleep(0.3)
            row = await storage.get("sess-U")
            assert row is not None
            assert row.parked_status == "parked"
        finally:
            await timer.stop()


@pytest.mark.asyncio
class TestTimeoutSweeper:
    async def test_sweeper_publishes_timeout_marker_for_expired_park(
        self, harness,
    ):
        bus, scheduler, storage, engine, _listener = harness
        # ask_user park (non-timer) whose deadline elapsed.
        sess = _make_parked_session(
            session_id="sess-S",
            event_key="ask_user:sess-S:tc-S",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await _seed_parked(storage, scheduler, sess)

        sweeper = TimeoutSweeper(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        sweeper.start()
        try:
            row = None
            for _ in range(50):
                await asyncio.sleep(0.02)
                row = await storage.get("sess-S")
                if row is not None and row.parked_status == "resumable":
                    break
            assert row is not None
            assert row.parked_status == "resumable"
            # Payload carries the timeout marker -- resume hook will
            # convert it to YieldTimeout via classify_resume_payload.
            assert row.parked_state is not None
            payload = row.parked_state["resume_event_payload"]
            assert payload.get("__yield_timeout__") is True
        finally:
            await sweeper.stop()

    async def test_sweeper_does_not_publish_for_timer_parks(self, harness):
        # Timer parks are the TimerScheduler's responsibility, not
        # the sweeper's. The sweeper only handles non-timer:* keys
        # so it doesn't double-publish.
        bus, scheduler, storage, engine, _listener = harness
        sess = _make_parked_session(
            session_id="sess-skip",
            event_key="timer:tc-skip",
            parked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        await _seed_parked(storage, scheduler, sess)

        sweeper = TimeoutSweeper(
            bus=bus, scheduler=scheduler, poll_seconds=0.05,
        )
        sweeper.start()
        try:
            await asyncio.sleep(0.2)
            # Sweeper ignored it (only the TimerScheduler would
            # have flipped it).
            row = await storage.get("sess-skip")
            assert row is not None
            assert row.parked_status == "parked"
        finally:
            await sweeper.stop()
