"""Integration tests for primer.session.dispatch.run_one_session_turn.

Each test boots a fake executor that emits a scripted StreamEvent sequence,
then verifies:
- The correct SessionMessageRecord kinds and payloads are persisted to
  messages.jsonl via FakeWorkspaceIO.
- The correct tick events are published to the EventBus.
- The ReleaseOutcome returned has the right success/drop_lease values.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.model.chat import Done, Error, TextDelta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_lease(session_id: str = "s1") -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION,
        entity_id=session_id,
        claimed_by="worker-1",
        claimed_at=now,
        expires_at=now,
        attempt_count=1,
        last_error=None,
    )


# ---------------------------------------------------------------------------
# FakeWorkspaceIO  (local copy — mirrors tests/session/test_persistence.py)
# ---------------------------------------------------------------------------


class FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str, filename: str = "messages.jsonl") -> list[str]:
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# FakeExecutor  — async generator that yields scripted StreamEvents
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Fake executor that emits a scripted list of StreamEvents."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def invoke(self, messages: list[Any], **kwargs: Any):
        for ev in self._events:
            if isinstance(ev, Exception):
                raise ev
            yield ev


# ---------------------------------------------------------------------------
# Session seeding helper
# ---------------------------------------------------------------------------


async def _seed_session(
    storage_provider,
    session_id: str = "s1",
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    storage = storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    return sess


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_workspace_io() -> FakeWorkspaceIO:
    return FakeWorkspaceIO()


@pytest.fixture
async def fake_event_bus():
    bus = InMemoryEventBus()
    await bus.initialize()
    yield bus
    await bus.aclose()


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider
    return _FakeStorageProvider()


@pytest.fixture
async def seeded_session(fake_storage_provider) -> WorkspaceSession:
    return await _seed_session(fake_storage_provider)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_session_turn_writes_assistant_token_and_done(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Happy-path: executor emits TextDeltas + Done → persists one
    ASSISTANT_TOKEN + one DONE; publishes 2 ticks."""
    import json

    fake_executor = FakeExecutor([
        TextDelta(text="hi", index=0),
        TextDelta(text=" there", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])

    collected_ticks: list[dict[str, Any]] = []

    async def _collect() -> None:
        sub = fake_event_bus.subscribe()
        try:
            async for event in sub:
                if event.event_key == f"session:{seeded_session.id}:tick":
                    collected_ticks.append(event.payload)
        except asyncio.CancelledError:
            pass
        finally:
            await sub.aclose()

    collector = asyncio.create_task(_collect())
    # Yield control so the collector coroutine gets to subscribe BEFORE
    # the dispatch publishes any tick events.
    await asyncio.sleep(0)

    async def _build_executor(session: WorkspaceSession):
        return fake_executor

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    # Give the collector task a moment to drain queued events
    await asyncio.sleep(0.05)
    collector.cancel()
    try:
        await collector
    except asyncio.CancelledError:
        pass

    assert outcome.success is True
    assert outcome.drop_lease is True

    lines = fake_workspace_io.read_lines(seeded_session.id)
    assert len(lines) == 2, f"expected 2 lines, got {len(lines)}: {lines}"

    r0 = json.loads(lines[0])
    r1 = json.loads(lines[1])
    assert r0["kind"] == SessionMessageKind.ASSISTANT_TOKEN
    assert r0["payload"]["text"] == "hi there"
    assert r1["kind"] == SessionMessageKind.DONE

    # One tick per record
    assert len(collected_ticks) == 2


@pytest.mark.asyncio
async def test_cancel_mid_stream_writes_cancelled_and_breaks(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Cancel published mid-stream: CANCELLED record written, no DONE."""
    import json

    # A gate that lets us pause the executor so cancel can arrive between events
    gate = asyncio.Event()

    class _SlowExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="starting…", index=0)
            # Block until the test opens the gate (by which time cancel is set)
            await gate.wait()
            # The dispatch loop should have already broken on cancel; this
            # event should never be persisted.
            yield TextDelta(text="should-not-persist", index=0)

    async def _build_executor(session: WorkspaceSession):
        return _SlowExecutor()

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)

    # Start the turn in the background so we can inject the cancel event
    turn_task = asyncio.create_task(run_one_session_turn(lease, deps))

    # Let the executor emit the first TextDelta, then fire cancel
    await asyncio.sleep(0.05)
    await fake_event_bus.publish(f"session:{seeded_session.id}:cancel", {})
    # Give the cancel watcher time to set the internal cancel_event flag
    await asyncio.sleep(0.05)
    # Now open the gate so the generator can advance — but cancel should
    # already be flagged, so the loop breaks before processing the next event.
    gate.set()

    outcome = await asyncio.wait_for(turn_task, timeout=2.0)

    assert outcome.success is True
    assert outcome.drop_lease is True

    lines = fake_workspace_io.read_lines(seeded_session.id)
    kinds = [json.loads(ln)["kind"] for ln in lines]
    assert SessionMessageKind.CANCELLED in kinds
    assert SessionMessageKind.DONE not in kinds


@pytest.mark.asyncio
async def test_yield_to_worker_writes_yielded_and_returns_no_drop(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """YieldToWorker exception → YIELDED record; drop_lease=False."""
    import json

    yielded = Yielded(tool_name="wait_tool", event_key="timer:tc1")
    exc = YieldToWorker(yielded, tool_call_id="tc1")

    class _YieldingExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="thinking", index=0)
            raise exc
            yield  # make it a generator

    async def _build_executor(session: WorkspaceSession):
        return _YieldingExecutor()

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True
    assert outcome.drop_lease is False

    lines = fake_workspace_io.read_lines(seeded_session.id)
    kinds = [json.loads(ln)["kind"] for ln in lines]
    assert SessionMessageKind.YIELDED in kinds
    assert SessionMessageKind.DONE not in kinds


@pytest.mark.asyncio
async def test_error_event_writes_error_record(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Executor emitting Error event → ERROR record persisted."""
    import json

    fake_executor = FakeExecutor([
        Error(message="boom", code="server_error", fatal=True),
    ])

    async def _build_executor(session: WorkspaceSession):
        return fake_executor

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True

    lines = fake_workspace_io.read_lines(seeded_session.id)
    kinds = [json.loads(ln)["kind"] for ln in lines]
    assert SessionMessageKind.ERROR in kinds


@pytest.mark.asyncio
async def test_each_record_gets_a_tick(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """One tick published per persisted SessionMessageRecord."""
    ticks: list[dict[str, Any]] = []

    async def _collect() -> None:
        sub = fake_event_bus.subscribe()
        try:
            async for event in sub:
                if event.event_key == f"session:{seeded_session.id}:tick":
                    ticks.append(event.payload)
        except asyncio.CancelledError:
            pass
        finally:
            await sub.aclose()

    collector = asyncio.create_task(_collect())
    # Yield control so the collector subscribes before dispatch publishes ticks.
    await asyncio.sleep(0)

    fake_executor = FakeExecutor([
        Done(stop_reason="stop", raw_reason="stop"),
    ])

    async def _build_executor(session: WorkspaceSession):
        return fake_executor

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    await run_one_session_turn(lease, deps)

    await asyncio.sleep(0.05)
    collector.cancel()
    try:
        await collector
    except asyncio.CancelledError:
        pass

    lines = fake_workspace_io.read_lines(seeded_session.id)
    # DONE only (no coalesced text)
    assert len(lines) == 1
    assert len(ticks) == 1
    assert ticks[0]["seq"] == 1
