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
async def test_pause_requested_on_entry_clears_stale_interrupt_requested(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """A row that carries pause_requested=True AND a stale
    interrupt_requested=True (left over from an earlier turn that never
    consumed it) must transition to PAUSED with interrupt_requested cleared
    — otherwise a later /resume could downgrade a genuine Cancel to a Stop."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    seeded_session.pause_requested = True
    seeded_session.pause_requested_at = _now()
    seeded_session.interrupt_requested = True
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
    assert outcome.preserve_park is True
    assert not executor_called, (
        "executor must NOT be built when pause_requested is set on entry"
    )
    row = await storage.get(seeded_session.id)
    assert row.status == SessionStatus.PAUSED
    assert row.interrupt_requested is False


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
    """After a clean turn (Done with stop_reason='stop'), the session's row
    MUST transition to ENDED/completed. Pre-fix the row stayed at RUNNING
    forever — a one-shot session would never end. Every clean turn now ends
    regardless of autonomy (see test_dispatch_interactive_alive.py and
    test_clean_completion_ends_for_interactive_session)."""
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


@pytest.mark.asyncio
async def test_clean_completion_ends_for_interactive_session(
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Regression: an INTERACTIVE (non-autonomous) agent session must ALSO
    transition to ENDED/completed after a clean turn. The old
    interactive-stays-WAITING downgrade hung every one-shot caller
    (triggers/webhooks/API/e2e) forever. A new message reopens the ended
    session via ``wake_session``."""
    session = await _seed_session(fake_storage_provider, autonomous=False)
    fake_executor = FakeExecutor([
        TextDelta(text="done", index=0),
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
    outcome = await run_one_session_turn(_make_lease(session.id), deps)
    assert outcome.success is True

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(session.id)
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "completed"


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


# ---------------------------------------------------------------------------
# Task 9 — Stop (interrupt) stays alive: WAITING, not ENDED
# ---------------------------------------------------------------------------


def test_interrupt_transitions_to_waiting_not_ended() -> None:
    from primer.session.dispatch import _interrupt_post_status

    assert _interrupt_post_status() == (SessionStatus.WAITING, None)


@pytest.mark.asyncio
async def test_interrupt_mid_stream_lands_waiting_and_clears_flag(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """Interrupt (Stop) published mid-stream: CANCELLED record written
    (reason='operator_interrupt'), but the row lands WAITING — alive —
    instead of ENDED, and interrupt_requested is cleared so the next
    turn is not re-interrupted."""
    import json

    # Flip the row's interrupt flag on disk — mirrors what the
    # /interrupt route does for a RUNNING session.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    seeded_session.interrupt_requested = True
    await storage.update(seeded_session)

    gate = asyncio.Event()

    class _SlowExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="starting…", index=0)
            await gate.wait()
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

    turn_task = asyncio.create_task(run_one_session_turn(lease, deps))

    await asyncio.sleep(0.05)
    await fake_event_bus.publish(f"session:{seeded_session.id}:cancel", {})
    await asyncio.sleep(0.05)
    gate.set()

    outcome = await asyncio.wait_for(turn_task, timeout=2.0)

    assert outcome.success is True
    assert outcome.drop_lease is True

    lines = fake_workspace_io.read_lines(seeded_session.id)
    records = [json.loads(ln) for ln in lines]
    kinds = [r["kind"] for r in records]
    assert SessionMessageKind.CANCELLED in kinds
    assert SessionMessageKind.DONE not in kinds
    cancelled = next(r for r in records if r["kind"] == SessionMessageKind.CANCELLED)
    assert cancelled["payload"]["reason"] == "operator_interrupt"

    row = await storage.get(seeded_session.id)
    assert row.status == SessionStatus.WAITING, (
        f"interrupted session left at {row.status!r} instead of WAITING"
    )
    assert row.ended_at is None
    assert row.interrupt_requested is False


@pytest.mark.asyncio
async def test_real_cancel_wins_over_stuck_interrupt_flag(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """I2 regression: a stale interrupt_requested=True (leaked from an
    earlier turn that never consumed it) must not downgrade a genuine
    concurrent Cancel to a Stop. Cancel is the stronger intent: when
    cancel_requested is ALSO true at the disambiguation point, the row
    must land ENDED/cancelled, not WAITING."""
    import json

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    # Stale interrupt flag, as if left over from a previous turn that
    # completed/parked/failed before ever consuming it.
    seeded_session.interrupt_requested = True
    await storage.update(seeded_session)

    gate = asyncio.Event()

    class _SlowExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="starting…", index=0)
            await gate.wait()
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

    turn_task = asyncio.create_task(run_one_session_turn(lease, deps))

    await asyncio.sleep(0.05)
    # A genuine hard cancel lands mid-turn (mirrors cancel_session()'s
    # cancel_requested=True write; both interrupt and cancel publish the
    # same "session:{id}:cancel" bus event).
    row = await storage.get(seeded_session.id)
    row.cancel_requested = True
    await storage.update(row)
    await fake_event_bus.publish(f"session:{seeded_session.id}:cancel", {})
    await asyncio.sleep(0.05)
    gate.set()

    outcome = await asyncio.wait_for(turn_task, timeout=2.0)

    assert outcome.success is True
    assert outcome.drop_lease is True

    lines = fake_workspace_io.read_lines(seeded_session.id)
    records = [json.loads(ln) for ln in lines]
    kinds = [r["kind"] for r in records]
    assert SessionMessageKind.CANCELLED in kinds

    # The functionally meaningful assertion: cancel must win the
    # disambiguation, landing the row ENDED/cancelled (terminal) rather
    # than WAITING (a Stop leaves the session alive). Note the CANCELLED
    # record's payload["reason"] text is not a reliable signal here: it's
    # sourced from a `cancel_reason` local that's hardcoded to
    # "operator_interrupt" on both branches (a pre-existing, unrelated
    # cosmetic gap) -- the row's status/ended_reason is the real contract.
    row = await storage.get(seeded_session.id)
    assert row.status == SessionStatus.ENDED, (
        f"cancel was downgraded to a stop: row left at {row.status!r} "
        "instead of ENDED"
    )
    assert row.ended_reason == "cancelled"


@pytest.mark.asyncio
async def test_interrupt_requested_cleared_after_clean_completion(
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """I2 regression: a stale interrupt_requested=True that this turn
    never consumed (e.g. it completed cleanly before any cancel event
    fired) must not survive past the turn -- every terminal path clears
    it, not just the interrupt-disambiguation branch, so it can't leak
    into and downgrade a future genuine Cancel."""
    session = await _seed_session(fake_storage_provider, autonomous=True)
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session.interrupt_requested = True
    await storage.update(session)

    fake_executor = FakeExecutor([
        Done(stop_reason="stop", raw_reason="stop"),
    ])

    async def _build_executor(_session: WorkspaceSession):
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
    row = await storage.get(session.id)
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "completed"
    assert row.interrupt_requested is False


# ---------------------------------------------------------------------------
# C1 — steering a RUNNING session must not strand the queued turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_start_consumes_claimable_and_mid_turn_steer_survives(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """C1 regression (dispatch-side half): run_one_session_turn must (a)
    consume ("idle") a turn_status="claimable" it started with -- proving
    a stale, already-serviced claimable can't spin-claim this session
    forever -- and (b) leave turn_status="claimable" on the row if a NEW
    wake_session() steer lands mid-turn, so the worker pool's
    post-release re-arm (WorkerPool._maybe_rearm_session, exercised
    end-to-end in tests/worker/test_pool.py) has a real signal to act on."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    seeded_session.turn_status = "claimable"
    await storage.update(seeded_session)

    seen_turn_status_at_start = None

    class _SteeringExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            nonlocal seen_turn_status_at_start
            # By the time the executor is invoked, the turn-start consume
            # step must already have cleared turn_status to "idle".
            row = await storage.get(seeded_session.id)
            seen_turn_status_at_start = row.turn_status
            yield TextDelta(text="thinking", index=0)
            # Simulate a wake_session() steer landing mid-turn (its
            # row-write half; the messages.jsonl append is irrelevant to
            # this assertion).
            fresh = await storage.get(seeded_session.id)
            fresh.turn_status = "claimable"
            await storage.update(fresh)
            yield Done(stop_reason="stop", raw_reason="stop")

    async def _build_executor(session: WorkspaceSession):
        return _SteeringExecutor()

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    assert outcome.success is True
    assert seen_turn_status_at_start == "idle", (
        "turn-start must consume ('idle') a claimable it started with, "
        f"but the executor saw {seen_turn_status_at_start!r}"
    )
    row = await storage.get(seeded_session.id)
    assert row.turn_status == "claimable", (
        "a steer landing mid-turn must survive to release so the pool's "
        "re-arm can see it"
    )


# ---------------------------------------------------------------------------
# Per-session seq monotonicity across turns (invoke + follow-up steer)
# ---------------------------------------------------------------------------
#
# Regression for the HIGH-severity per-session seq bug: dispatch created the
# per-turn WorkspaceMessageWriter with NO start_seq (each turn restarted at
# seq=1) and never persisted the advancing seq back to the row. That collided
# every turn's records with wake_session's USER_INPUT seqs and with the prior
# turn's records, so the seq-filtered tap path silently dropped physically
# later records whose seq <= a previously-seen value.


class _SeqFakeSlot:
    """On-disk slot stub: records the FIFO instructions wake_session appends."""

    def __init__(self) -> None:
        self.appended: list[str] = []
        self.reopened = 0

    async def append_instruction(self, content: str) -> None:
        self.appended.append(content)

    async def reopen(self) -> None:
        self.reopened += 1


class _BridgingWorkspaceIO:
    """Workspace IO serving BOTH surfaces the seq path touches.

    * ``append_message_line`` — used by the WorkspaceMessageWriter that both
      ``wake_session`` (USER_INPUT rows) and ``dispatch`` (turn output) drive.
    * ``read_file`` + ``state_path`` + ``get_session`` — the tap reader's
      seq-filtered live path (``read_session_since``) and wake's slot lookup.

    Both writers and the reader target the same ``messages.jsonl`` bytes so a
    tap client sees exactly what the two turns persisted.
    """

    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._slot = _SeqFakeSlot()

    def _path(self, session_id: str) -> str:
        return f"{self.state_path}/sessions/{session_id}/messages.jsonl"

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        p = self._path(session_id)
        self._files[p] = self._files.get(p, b"") + line

    async def read_file(self, path: str) -> bytes:
        from primer.model.except_ import NotFoundError

        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def get_session(self, session_id: str) -> _SeqFakeSlot:
        return self._slot

    def read_records(self, session_id: str) -> list[dict[str, Any]]:
        import json

        raw = self._files.get(self._path(session_id), b"")
        return [
            json.loads(ln) for ln in raw.decode().splitlines() if ln.strip()
        ]


class _SeqFakeRegistry:
    def __init__(self, ws: _BridgingWorkspaceIO) -> None:
        self._ws = ws

    async def get_workspace(self, workspace_id: str) -> _BridgingWorkspaceIO:
        return self._ws


class _SeqFakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


class _SeqFakeEngine:
    async def upsert(self, kind, session_id, *, priority=100, next_attempt_at=None):
        return None


@pytest.mark.asyncio
async def test_seq_strictly_increases_across_invoke_and_restart(
    fake_storage_provider,
    fake_event_bus: InMemoryEventBus,
) -> None:
    """Two turns on ONE session (first invoke + follow-up message after the
    first turn ends) must persist strictly-increasing unique seqs, and a
    seq-filtered tap reader that already consumed turn 1 must surface ALL of
    turn 2 (none dropped).

    Every clean turn now ENDS the session, so the follow-up message is a
    restart (``wake_session``'s ENDED branch reopens: it writes an
    INVOCATION_DIVIDER before the turn-2 USER_INPUT) rather than a live
    steer — the seq-monotonicity invariant must hold across that boundary too.

    FAILS before the dispatch seed+persist fix (turn writers restart at
    seq=1, colliding with the USER_INPUT rows and each other, so the
    ``after_seq`` reader drops turn 2 entirely).
    """
    from primer.session.enqueue import SessionWakeDeps, wake_session
    from primer.tap.reader import read_session_since
    from primer.tap.selector import TapSelector

    io = _BridgingWorkspaceIO()
    registry = _SeqFakeRegistry(io)
    scheduler = _SeqFakeScheduler()
    engine = _SeqFakeEngine()

    session_id = "s-seq"
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        WorkspaceSession(
            id=session_id,
            workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.CREATED,
            # Every clean turn ends the session; the follow-up message (turn 2)
            # restarts it via wake_session's ENDED reopen branch.
            autonomous=False,
            created_at=_now(),
            turn_status="idle",
        )
    )

    wake_deps = SessionWakeDeps(
        storage_provider=fake_storage_provider,
        scheduler=scheduler,
        claim_engine=engine,
        workspace_registry=registry,
        event_bus=fake_event_bus,
    )

    def _dispatch_deps(events: list[Any]) -> SessionDispatchDeps:
        async def _build(session: WorkspaceSession):
            return FakeExecutor(events)

        return SessionDispatchDeps(
            storage_provider=fake_storage_provider,
            workspace_io=io,
            event_bus=fake_event_bus,
            build_executor=_build,
        )

    # --- Turn 1: first invoke ---
    await wake_session(
        workspace_id="w1", session_id=session_id,
        instruction="first", deps=wake_deps,
    )
    await run_one_session_turn(
        _make_lease(session_id),
        _dispatch_deps([
            TextDelta(text="alpha", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]),
    )
    row = await storage.get(session_id)
    assert row.status == SessionStatus.ENDED, (
        "every clean turn ends the session; a follow-up message restarts it"
    )
    assert row.ended_reason == "completed"

    # --- Turn 2: follow-up message restarts the ended session ---
    await wake_session(
        workspace_id="w1", session_id=session_id,
        instruction="second", deps=wake_deps,
    )
    await run_one_session_turn(
        _make_lease(session_id),
        _dispatch_deps([
            TextDelta(text="beta", index=0),
            Done(stop_reason="stop", raw_reason="stop"),
        ]),
    )

    # --- All persisted records: strictly increasing, unique seqs ---
    recs = io.read_records(session_id)
    seqs = [r["seq"] for r in recs]
    assert seqs == sorted(seqs), f"seqs not strictly increasing in file order: {seqs}"
    assert len(seqs) == len(set(seqs)), f"duplicate seqs across turns: {seqs}"
    # turn 1: USER_INPUT(first), ASSISTANT_TOKEN, DONE
    # turn 2 (restart): INVOCATION_DIVIDER, USER_INPUT(second), ASSISTANT_TOKEN, DONE
    assert len(recs) == 7, f"expected 7 records, got {len(recs)}: {[r['kind'] for r in recs]}"
    assert [r["kind"] for r in recs].count(SessionMessageKind.USER_INPUT) == 2
    assert (
        [r["kind"] for r in recs].count(SessionMessageKind.INVOCATION_DIVIDER) == 1
    )

    # --- Seq-filtered tap: a client that consumed turn 1 sees ALL of turn 2 ---
    row = await storage.get(session_id)
    turn1 = recs[:3]
    turn2 = recs[3:]
    high_water = max(r["seq"] for r in turn1)
    events, _ = await read_session_since(
        io,
        workspace_id="w1",
        session=row,
        after_seq=high_water,
        selector=TapSelector(),
        from_offset=0,
    )
    surfaced = sorted(e.seq for e in events)
    assert len(events) == len(turn2), (
        "seq-filtered tap dropped physically-later turn-2 records: "
        f"surfaced {surfaced} after after_seq={high_water}"
    )
    assert min(surfaced) > high_water


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Residual mid-turn concurrent-steer race: wake_session's USER_INPUT "
        "writer and the in-flight turn writer own INDEPENDENT seq counters. A "
        "steer landing DURING a running turn (before that turn persists its "
        "advancing last_seq at turn end) reads a stale row.last_seq and its "
        "USER_INPUT seq collides with the turn's still-buffered output. "
        "Persisting the row per-flush does not help — the two counters can't "
        "be reconciled without a single shared per-session seq source (both "
        "writers incrementing row.last_seq under the lifecycle lock per "
        "record), which is a WorkspaceMessageWriter redesign tracked as a "
        "follow-up. This test marks that boundary and will flip to a failing "
        "XPASS once the shared-seq-source fix lands."
    ),
)
@pytest.mark.asyncio
async def test_midturn_concurrent_steer_seq_collision_is_known_gap(
    fake_storage_provider,
    fake_event_bus: InMemoryEventBus,
) -> None:
    """Documents the residual mid-turn-concurrent-steer seq collision.

    A steer that lands WHILE turn 1 is streaming (its writer counter still
    mid-flight, last_seq not yet persisted) writes a USER_INPUT whose seq
    collides with turn 1's output. Strictly-increasing unique seqs therefore
    do NOT hold — the known gap the turn-boundary fix does not close.
    """
    from primer.session.enqueue import SessionWakeDeps, wake_session

    io = _BridgingWorkspaceIO()
    wake_deps = SessionWakeDeps(
        storage_provider=fake_storage_provider,
        scheduler=_SeqFakeScheduler(),
        claim_engine=_SeqFakeEngine(),
        workspace_registry=_SeqFakeRegistry(io),
        event_bus=fake_event_bus,
    )

    session_id = "s-seq-mid"
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        WorkspaceSession(
            id=session_id,
            workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.CREATED,
            autonomous=False,
            created_at=_now(),
            turn_status="idle",
        )
    )

    # Turn 1 invoke.
    await wake_session(
        workspace_id="w1", session_id=session_id,
        instruction="first", deps=wake_deps,
    )

    async def _steer_mid_turn() -> None:
        await wake_session(
            workspace_id="w1", session_id=session_id,
            instruction="steer-mid", deps=wake_deps,
        )

    class _MidTurnSteerExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="x", index=0)
            # A concurrent steer lands mid-turn: wake_session writes a
            # USER_INPUT record now, while this turn's buffered text is not
            # yet flushed and its writer counter has not advanced.
            await _steer_mid_turn()
            yield Done(stop_reason="stop", raw_reason="stop")

    async def _build(session: WorkspaceSession):
        return _MidTurnSteerExecutor()

    await run_one_session_turn(
        _make_lease(session_id),
        SessionDispatchDeps(
            storage_provider=fake_storage_provider,
            workspace_io=io,
            event_bus=fake_event_bus,
            build_executor=_build,
        ),
    )

    seqs = [r["seq"] for r in io.read_records(session_id)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), (
        f"mid-turn steer produced non-monotonic/duplicate seqs: {seqs}"
    )
