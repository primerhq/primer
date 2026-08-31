"""Verifies the five turn-log hooks fire on the right dispatch paths.

Each hook must land BEFORE the existing message-record write so the
operator's UI sees the structured turn-log on the same WS poll. The
failed hook replaces the dispatch's generic 'unexpected executor error'
string with a real ProblemDetails envelope.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from primer.api.errors import ProblemDetails
from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, TextDelta
from primer.model.except_ import NetworkError
from primer.model.turn_log import TurnLogKind
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.observability.turn_log_writer import TurnLogWriter


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


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data.setdefault(session_id, b"")
        self._data[session_id] += line


class _CapturingTurnLogWriter(TurnLogWriter):
    def __init__(self) -> None:
        self.events: list = []
        self.closed = False
        self._seq = 0

    async def append(self, event):
        self._seq += 1
        # Stamp the captured copy with the seq so tests can inspect it.
        self.events.append(event.model_copy(update={"seq": self._seq}))
        return self._seq

    async def aclose(self):
        self.closed = True


class _FakeExecutor:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def invoke(self, messages, **kwargs):
        for ev in self._events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev


async def _seed_session(
    storage_provider, *, parked_at: datetime | None = None,
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id="s1",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
        parked_at=parked_at,
    )
    storage = storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    return sess


@pytest.fixture
def fake_workspace_io() -> _FakeWorkspaceIO:
    return _FakeWorkspaceIO()


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


def _build_deps(
    storage_provider,
    workspace_io,
    event_bus,
    executor,
    *,
    turn_log_writer: TurnLogWriter,
) -> SessionDispatchDeps:
    async def _build_executor(sess):
        return executor

    return SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=workspace_io,
        event_bus=event_bus,
        build_executor=_build_executor,
        turn_log_writer_factory=lambda io, sid: turn_log_writer,
    )


@pytest.mark.asyncio
async def test_started_then_completed(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success
    kinds = [e.kind for e in writer.events]
    assert TurnLogKind.STARTED in kinds
    assert TurnLogKind.COMPLETED in kinds
    assert kinds.index(TurnLogKind.STARTED) < kinds.index(TurnLogKind.COMPLETED)
    # The completed event carries a duration_ms.
    completed = next(e for e in writer.events if e.kind == TurnLogKind.COMPLETED)
    assert completed.duration_ms >= 0
    assert writer.closed


@pytest.mark.asyncio
async def test_failed_carries_problem_details(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([NetworkError("Connection reset by peer")])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success is False
    kinds = [e.kind for e in writer.events]
    assert TurnLogKind.STARTED in kinds
    assert TurnLogKind.FAILED in kinds
    failed = next(e for e in writer.events if e.kind == TurnLogKind.FAILED)
    assert isinstance(failed.error, ProblemDetails)
    assert failed.error.status == 504
    assert failed.error.title == "Network Error"
    assert "Connection reset by peer" in failed.error.detail
    assert failed.error.extensions["exception_class"] == "NetworkError"


@pytest.mark.asyncio
async def test_failed_message_record_carries_problem_details(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """The messages.jsonl ERROR record now carries the same structured
    error info as the turn-log event -- no more generic
    'unexpected executor error' string. Operators on the Messages tab
    see exception type/title/detail/status without having to switch."""
    import json

    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([NetworkError("Connection reset by peer")])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    await run_one_session_turn(_make_lease(sess.id), deps)

    raw = fake_workspace_io._data[sess.id]
    lines = [
        json.loads(line)
        for line in raw.decode().splitlines()
        if line.strip()
    ]
    error_recs = [r for r in lines if r["kind"] == "error"]
    assert len(error_recs) == 1
    payload = error_recs[0]["payload"]
    # New fields populated from the ProblemDetails envelope.
    assert payload["title"] == "Network Error"
    assert payload["status"] == 504
    assert payload["code"] == "/errors/network-error"
    assert "Connection reset by peer" in payload["message"]
    assert payload["extensions"]["exception_class"] == "NetworkError"


@pytest.mark.asyncio
async def test_yielded_event_fires_before_park(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(tool_name="ask_user", event_key="ask_user:s1:tc1")
    park = YieldToWorker(yielded, tool_call_id="tc1")
    executor = _FakeExecutor([park])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success
    # F10c fix: park outcome drops the lease so the session isn't re-claimed.
    assert outcome.drop_lease is True
    assert outcome.park is not None
    assert outcome.park.parked_event_key == "ask_user:s1:tc1"
    kinds = [e.kind for e in writer.events]
    assert TurnLogKind.STARTED in kinds
    assert TurnLogKind.YIELDED in kinds
    y = next(e for e in writer.events if e.kind == TurnLogKind.YIELDED)
    assert y.event_key == "ask_user:s1:tc1"
    assert y.yield_kind == "ask_user"


@pytest.mark.asyncio
async def test_cancelled_event_fires(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """A long-running stream + a mid-stream cancel signal lands a CANCELLED
    turn-log event after the existing CANCELLED message record write."""
    sess = await _seed_session(fake_storage_provider)

    async def _slow_invoke():
        yield TextDelta(text="a", index=0)
        await asyncio.sleep(0.5)
        yield TextDelta(text="b", index=0)

    class _SlowExecutor:
        async def invoke(self, messages, **kwargs):
            async for ev in _slow_invoke():
                yield ev

    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        _SlowExecutor(), turn_log_writer=writer,
    )
    turn_task = asyncio.create_task(
        run_one_session_turn(_make_lease(sess.id), deps),
    )
    await asyncio.sleep(0.05)
    await fake_event_bus.publish(f"session:{sess.id}:cancel", {})
    outcome = await turn_task
    assert outcome.success
    kinds = [e.kind for e in writer.events]
    assert TurnLogKind.CANCELLED in kinds


@pytest.mark.asyncio
async def test_resumed_fires_before_started_when_parked(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    parked = _now() - timedelta(seconds=3.5)
    sess = await _seed_session(fake_storage_provider, parked_at=parked)
    executor = _FakeExecutor([
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    await run_one_session_turn(_make_lease(sess.id), deps)
    kinds = [e.kind for e in writer.events]
    assert TurnLogKind.RESUMED in kinds
    assert TurnLogKind.STARTED in kinds
    assert kinds.index(TurnLogKind.RESUMED) < kinds.index(TurnLogKind.STARTED)
    resumed = next(e for e in writer.events if e.kind == TurnLogKind.RESUMED)
    assert resumed.wait_ms >= 3000


@pytest.mark.asyncio
async def test_yielded_tool_approval_maps_to_approval_kind(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """Real tool-approval yields land with event_key prefix
    'tool_approval:...'; the turn-log yield_kind must be 'approval'."""
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(
        tool_name="_approval",
        event_key="tool_approval:s1:tc1",
    )
    park = YieldToWorker(yielded, tool_call_id="tc1")
    executor = _FakeExecutor([park])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    await run_one_session_turn(_make_lease(sess.id), deps)
    y = next(e for e in writer.events if e.kind == TurnLogKind.YIELDED)
    assert y.yield_kind == "approval"
    assert y.event_key == "tool_approval:s1:tc1"


@pytest.mark.asyncio
async def test_yielded_timer_maps_to_subscribe_to_trigger(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    sess = await _seed_session(fake_storage_provider)
    yielded = Yielded(tool_name="sleep", event_key="timer:tc1")
    park = YieldToWorker(yielded, tool_call_id="tc1")
    executor = _FakeExecutor([park])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    await run_one_session_turn(_make_lease(sess.id), deps)
    y = next(e for e in writer.events if e.kind == TurnLogKind.YIELDED)
    assert y.yield_kind == "subscribe_to_trigger"


@pytest.mark.asyncio
async def test_default_writer_is_noop_when_factory_not_supplied(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """Backwards compat: an old SessionDispatchDeps construction (no
    turn_log_writer_factory kwarg) defaults to NoopTurnLogWriter so the
    dispatch runs end-to-end without complaint."""
    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([
        Done(stop_reason="stop", raw_reason="stop"),
    ])

    async def _build_executor(s):
        return executor

    # Omit turn_log_writer_factory; the dataclass default takes over.
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success


# ---------------------------------------------------------------------------
# agent_phase (01a04d91-a7a0, PHASE 1 of the execution-lifecycle revamp)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_sequence_thinking_responding_waiting(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """A plain text-only turn (no tool call) must record the phase
    sequence thinking -> responding -> waiting - "thinking" is written at
    turn start (before any StreamEvent arrives), "responding" the instant
    the first TextDelta arrives, "waiting" on Done."""
    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success

    phases = [e.phase for e in writer.events if e.kind == TurnLogKind.PHASE]
    assert phases == ["thinking", "responding", "waiting"]
    # Every phase entry is fenced to this turn.
    assert all(
        e.turn_no == sess.turn_no
        for e in writer.events if e.kind == TurnLogKind.PHASE
    )

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sess.id)
    # The row is cleared back to None by the same finally that clears
    # turn_status to idle - agent_phase is scoped to "while running".
    assert row.agent_phase is None
    assert row.agent_phase_turn_no is None
    assert row.agent_phase_stamped_at is None


@pytest.mark.asyncio
async def test_phase_sequence_with_tool_call_visits_executing(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """A tool-calling turn must visit "executing" for the tool call and
    return to "thinking" once it ends (about to re-request a completion
    with the tool result), before the eventual "responding" + "waiting"
    of the final answer."""
    from primer.model.chat import ReasoningDelta, ToolCallEnd, ToolCallStart

    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([
        ReasoningDelta(text="let me check", index=0),
        ToolCallStart(id="tc1", name="misc__uuid_v4", index=0),
        ToolCallEnd(id="tc1", arguments={}, index=0),
        TextDelta(text="done", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success

    phases = [e.phase for e in writer.events if e.kind == TurnLogKind.PHASE]
    # "thinking" at start is not re-emitted for the ReasoningDelta (no
    # change from the initial value) - only genuine transitions land.
    assert phases == ["thinking", "executing", "thinking", "responding", "waiting"]


@pytest.mark.asyncio
async def test_phase_visible_on_the_row_mid_stream(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """A client polling the row mid-turn (not just the tap/audit trail)
    must see the CURRENT phase - this is the field a refresh can recover
    from without any live-frame dependency."""
    gate = asyncio.Event()

    class _BlockedExecutor:
        async def invoke(self, messages, **kwargs):
            yield TextDelta(text="starting", index=0)
            await gate.wait()
            yield Done(stop_reason="stop", raw_reason="stop")

    sess = await _seed_session(fake_storage_provider)
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        _BlockedExecutor(), turn_log_writer=writer,
    )
    turn_task = asyncio.create_task(run_one_session_turn(_make_lease(sess.id), deps))
    await asyncio.sleep(0.05)

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    mid_row = await storage.get(sess.id)
    assert mid_row.agent_phase == "responding"
    assert mid_row.agent_phase_turn_no == sess.turn_no
    assert mid_row.agent_phase_stamped_at is not None

    gate.set()
    outcome = await asyncio.wait_for(turn_task, timeout=2.0)
    assert outcome.success


@pytest.mark.asyncio
async def test_phase_transitions_to_waiting_on_park(
    fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """YieldToWorker is a Python exception, never a StreamEvent, so
    infer_agent_phase never sees it directly - the park branch must emit
    its own explicit "waiting" transition rather than silently leaving
    whatever phase was active (e.g. "executing") to just vanish into the
    finally block's reset-to-None with no audit trail of why."""
    yielded = Yielded(tool_name="ask_user", event_key="ask_user:s1:tc1")
    park = YieldToWorker(yielded, tool_call_id="tc1")
    sess = await _seed_session(fake_storage_provider)
    executor = _FakeExecutor([TextDelta(text="hi", index=0), park])
    writer = _CapturingTurnLogWriter()
    deps = _build_deps(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        executor, turn_log_writer=writer,
    )
    outcome = await run_one_session_turn(_make_lease(sess.id), deps)
    assert outcome.success

    phases = [e.phase for e in writer.events if e.kind == TurnLogKind.PHASE]
    assert phases == ["thinking", "responding", "waiting"]
