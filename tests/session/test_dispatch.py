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
    *,
    autonomous: bool | None = None,
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        autonomous=autonomous,
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
    """YieldToWorker exception -> YIELDED record; drop_lease=True, park populated."""
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
    # F10c fix: lease must be dropped and park columns populated so the
    # session is not re-claimed in an infinite loop.
    assert outcome.drop_lease is True
    assert outcome.park is not None
    assert outcome.park.parked_event_key == "timer:tc1"

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


@pytest.mark.asyncio
async def test_cancel_requested_on_entry_ends_without_executor(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """A row that already carries cancel_requested=True must transition
    to ENDED/cancelled at the start of dispatch — the executor must
    never be invoked. This is what makes a cancel issued before the
    api restart actually land after recovery."""
    # Flip the row's cancel flag on disk.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    seeded_session.cancel_requested = True
    seeded_session.cancel_requested_at = _now()
    await storage.update(seeded_session)

    executor_called = False

    async def _build_executor(session: WorkspaceSession):
        nonlocal executor_called
        executor_called = True
        return FakeExecutor([])

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True
    assert outcome.drop_lease is True
    assert not executor_called, (
        "executor must NOT be built when cancel_requested is set on entry"
    )
    # Row transitioned to ENDED/cancelled.
    row = await storage.get(seeded_session.id)
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "cancelled"
    assert row.ended_at is not None


@pytest.mark.asyncio
async def test_already_ended_row_drops_lease_without_executor(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """A row that's already ENDED on entry must not be re-executed."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    seeded_session.status = SessionStatus.ENDED
    seeded_session.ended_reason = "completed"
    seeded_session.ended_at = _now()
    await storage.update(seeded_session)

    executor_called = False

    async def _build_executor(session: WorkspaceSession):
        nonlocal executor_called
        executor_called = True
        return FakeExecutor([])

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.drop_lease is True
    assert not executor_called


@pytest.mark.asyncio
async def test_clean_completion_transitions_to_ended_completed(
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """After a clean turn (Done with stop_reason='stop'), an AUTONOMOUS
    session's row MUST transition to ENDED/completed. Pre-fix the row
    stayed at RUNNING forever — a one-shot session would never end.
    (Seeded ``autonomous=True`` — an interactive agent session now stays
    WAITING instead; see test_dispatch_interactive_alive.py.)"""
    session = await _seed_session(fake_storage_provider, autonomous=True)
    fake_executor = FakeExecutor([
        TextDelta(text="4", index=0),
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
    lease = _make_lease(session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(session.id)
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "completed"
    assert row.ended_at is not None


class _RecordingSlotSession:
    """Minimal AgentSession stand-in for the slot-mirror assertion."""

    def __init__(self) -> None:
        self._status = SessionStatus.RUNNING
        self.calls: list[tuple] = []

    async def status(self):
        return self._status

    async def set_status(self, status, *, ended_reason=None, **_kw):
        self.calls.append((status, ended_reason))
        self._status = status


class _ExecutorWithSession(FakeExecutor):
    def __init__(self, events, session) -> None:
        super().__init__(events)
        self.session = session


@pytest.mark.asyncio
async def test_clean_completion_mirrors_ended_onto_agent_session_slot(
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """The dispatch terminal transition mirrors ENDED onto the executor's
    on-disk AgentSession slot, so the workspace-side reads
    (``get_workspace_session`` / ``list_*``) don't report a finished
    worker-run session as still ``running``. The executor leaves its
    AgentSession at RUNNING after a clean ``stop`` (dispatch decides ENDED),
    so without the mirror the slot would stay RUNNING forever.
    (Seeded ``autonomous=True`` so this AUTONOMOUS-session case is unaffected
    by the interactive-stays-WAITING behaviour.)
    """
    session = await _seed_session(fake_storage_provider, autonomous=True)
    slot = _RecordingSlotSession()
    fake_executor = _ExecutorWithSession(
        [TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")],
        slot,
    )

    async def _build_executor(session: WorkspaceSession):
        return fake_executor

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    outcome = await run_one_session_turn(_make_lease(session.id), deps)
    assert outcome.success is True
    # The scheduler row AND the on-disk slot both reached ENDED/completed.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    assert (await storage.get(session.id)).status == SessionStatus.ENDED
    assert slot.calls == [(SessionStatus.ENDED, "completed")]


@pytest.mark.asyncio
async def test_executor_error_transitions_to_ended_failed(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """When the executor raises, the row must transition to ENDED/failed."""
    fake_executor = FakeExecutor([RuntimeError("kaboom")])

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

    assert outcome.success is False
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(seeded_session.id)
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "failed"


@pytest.mark.asyncio
async def test_executor_error_plus_write_failure_still_ends_session(
    seeded_session: WorkspaceSession,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Failure isolation: LLM raises AND workspace IO also raises on the
    error-record write.  The session MUST still transition to ENDED/failed
    and the lease MUST be dropped (drop_lease=True).

    Pre-fix the secondary IOError escaped the except block before
    _transition_session_status ran, leaving the session stuck RUNNING
    with no lease (t0539/t0630/t0649/t0679).
    """

    class _BrokenWorkspaceIO:
        """IO that always raises on append - simulates disk/mount failure."""

        async def append_message_line(self, session_id: str, line: bytes) -> None:
            raise OSError("disk full")

    broken_io = _BrokenWorkspaceIO()
    fake_executor = FakeExecutor([RuntimeError("llm exploded")])

    async def _build_executor(session: WorkspaceSession):
        return fake_executor

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=broken_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    # Lease must always be dropped, even when the error-record write failed.
    assert outcome.drop_lease is True

    # Session must have transitioned to ENDED/failed, not left RUNNING.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(seeded_session.id)
    assert row is not None
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "failed"


@pytest.mark.asyncio
async def test_build_executor_notfound_converges_to_ended_failed(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Build-time fatal: ``build_executor`` raises (e.g. a graph-bound
    session whose graph row was deleted -> NotFoundError at resolve).

    The session MUST converge to ENDED/failed, never left stuck RUNNING.
    Regression for e2e t0624: a graph-bound CREATED session whose graph
    is deleted, then resumed, hung at status=running because the
    ``build_executor`` call sat OUTSIDE the fatal try/except and the
    exception escaped run_one_session_turn entirely (the worker's
    _run_engine_session only logged it without transitioning the row).
    """
    from primer.model.except_ import NotFoundError

    async def _build_executor(session: WorkspaceSession):
        raise NotFoundError(
            f"Graph 'g-gone' not found for session {session.id!r}"
        )

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    # The lease must always be dropped so the engine doesn't re-claim.
    assert outcome.drop_lease is True

    # The session must have transitioned to ENDED/failed, not left RUNNING.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(seeded_session.id)
    assert row is not None
    assert row.status == SessionStatus.ENDED, (
        f"session left at {row.status!r} instead of ENDED"
    )
    assert row.ended_reason == "failed"
