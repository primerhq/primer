"""A workspace that never answers must not hold a turn's exit at its last flush (ticket 01a11b58).

``_flush_and_tick`` made the turn's last records durable with ``writer.flush()``, which waits without limit for the batch the writer
keeps in flight. On the clean-completion exit, the failure exit and both park exits that wait held the lease release and the
session's lifecycle (the cancelled exit has bounds of its own, but each of them waited the full time for the same dead batch, one
after the other).

Here the workspace stops answering and every exit is run through ``run_one_session_turn``: it must land, within the bound, with the loss
logged and the tick published. The turn log is the PRODUCTION shape (``WorkspaceTurnLogWriter`` over the same workspace: its appends and
its first-append bootstrap read go over the same dead connection), not the no-op the dispatch defaults to. Every test body is bounded: no
path may hang the lane.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch
from primer.model.chat import Done, ExtendedEvent, TextDelta, ToolCallEnd, ToolCallStart, _ExecutorToolResult
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.observability.turn_log_writer import WorkspaceTurnLogWriter
from primer.session import persistence
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _build_returning, _request_stop, _StopAwareExecutor

HARD_BOUND_S = 3.0     # the whole body of a test: the turn runs through run_one_session_turn
WRITE_BOUND_S = 0.2    # the writer's bound, and the turn log's


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


class _Workspace(FakeWorkspaceIO):
    """A workspace whose runtime connection can drop: while ``hung`` every call over it waits for ``release``, the message log's
    appends and the turn log's appends and reads alike (they share the connection)."""

    def __init__(self) -> None:
        super().__init__()
        self.hung = False
        self.release = asyncio.Event()
        self.hung_calls = 0

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        if self.hung:
            self.hung_calls += 1
            await self.release.wait()
        await super().append_message_line(session_id, line)

    async def append_state_line(self, workspace_id: str, relative_path: str, line: bytes) -> None:
        if self.hung:
            await self.release.wait()

    async def read_state_file(self, workspace_id: str, relative_path: str) -> bytes:
        if self.hung:
            await self.release.wait()
        return b""


def _production_turn_log(io: _Workspace):
    """What ``WorkerPool._turn_log_factory`` builds: a ``WorkspaceTurnLogWriter`` whose appends and bootstrap read go through the
    workspace shim."""

    def factory(_workspace_io: Any, session_id: str) -> WorkspaceTurnLogWriter:
        rel = f"sessions/{session_id}/turns.jsonl"

        async def append(line: bytes) -> None:
            await io.append_state_line("ws-1", rel, line)

        async def read_existing() -> bytes:
            return await io.read_state_file("ws-1", rel)

        return WorkspaceTurnLogWriter(append_line=append, read_existing=read_existing)

    return factory


@pytest.fixture
def bounded_writes(monkeypatch):
    # raising=False: before the bound exists the setting is inert and the tests fail by hanging, which the body bound reports
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", WRITE_BOUND_S, raising=False)
    monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", WRITE_BOUND_S)


async def _boom() -> None:
    raise RuntimeError("the model call blew up")


async def _let_the_buffer_age() -> None:
    """The tool_call record has now sat in the writer's buffer past its 100 ms age limit, as it does while a real tool runs."""
    await asyncio.sleep(0.15)


def _park(session_id: str):
    async def park() -> None:
        raise YieldToWorker(
            Yielded(tool_name="ask_user", event_key=f"ask_user:{session_id}:tc1", resume_metadata={"prompt": "which one?"}),
            tool_call_id="tc1",
        )

    return park


class _Published:
    def __init__(self, bus) -> None:
        self.keys: list[str] = []
        real = bus.publish

        async def spy(key: str, payload: dict) -> None:
            self.keys.append(key)
            await real(key, payload)

        bus.publish = spy


def _deps(storage, io, bus, executor, **extra: Any) -> SessionDispatchDeps:
    return SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=_build_returning(executor),
        turn_log_writer_factory=_production_turn_log(io), **extra,
    )


@pytest.mark.parametrize("exit_kind", ["done", "failed", "parked", "parked_after_a_tool_call"])
async def test_a_turn_exit_lands_when_the_workspace_never_answers_its_last_flush(
    seeded_session, fake_event_bus, fake_storage_provider, bounded_writes, caplog, exit_kind,
) -> None:
    """``parked_after_a_tool_call`` is the NORMAL park shape: the tool_call record is buffered when the tool starts and the tool
    runs longer than the age limit, so the park's own ``append`` runs the age flush, and THAT is what meets the dead batch."""
    sid = seeded_session.id
    io = _Workspace()
    io.hung = True                       # the connection dropped before the turn's records were flushed
    published = _Published(fake_event_bus)
    tool_call = [ToolCallStart(id="t1", name="x", index=0), ToolCallEnd(id="t1", arguments={}, index=0)]
    script = {
        "done": [TextDelta(text="hello", index=0), Done(stop_reason="stop", raw_reason="stop")],
        "failed": [TextDelta(text="hello", index=0), _boom],
        "parked": [TextDelta(text="hello", index=0), _park(sid)],
        "parked_after_a_tool_call": [TextDelta(text="hello", index=0), *tool_call, _let_the_buffer_age, _park(sid)],
    }[exit_kind]
    deps = _deps(fake_storage_provider, io, fake_event_bus, _StopAwareExecutor(script))
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            with caplog.at_level(logging.WARNING):
                outcome = await run_one_session_turn(_make_lease(sid), deps)
    finally:
        io.release.set()

    assert io.hung_calls >= 1, "the turn never tried to flush: the test is not in its situation"
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    if exit_kind.startswith("parked"):
        assert outcome.park is not None, "the turn did not park (its release dropped the lease instead)"
    elif exit_kind == "failed":
        assert row.status == SessionStatus.ENDED and row.ended_reason == "failed"
    else:
        assert outcome.success and row.status != SessionStatus.RUNNING
    assert f"session:{sid}:tick" in published.keys, "the exit never published the tick that follows its flush"
    text = " | ".join(r.getMessage() for r in caplog.records)
    assert "did not accept" in text, f"the lost records were not logged: {text}"


class _Claims:
    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        return None


async def test_a_tool_wait_park_lands_when_the_workspace_stops_answering_after_the_tool_call_was_flushed(
    fake_event_bus, fake_storage_provider, bounded_writes, caplog,
) -> None:
    """The claims seam flushes the tool_call record at once (a claim worker reads the log from another process), so the workspace dies
    AFTER that. A record buffered since then is older than the age limit when the park appends its YIELDED record: the append runs
    the age flush and meets the dead batch inside the park arm's handler, where nothing caught it."""
    io = _Workspace()
    sid = "s-tool-wait"
    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=dispatch._now(), turn_status="running",
    ))

    class _ToolWaitExecutor:
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            yield ToolCallStart(id="call_a", name="tool_a", index=0)
            yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)       # flushed at once, the workspace is still up
            yield ExtendedEvent(extended=_ExecutorToolResult(call_id="call_a", output="ok", error=False))   # buffered
            io.hung = True
            await asyncio.sleep(0.15)
            raise ToolWaitPark(outstanding_task_ids=["x:tool:0:1"], event_key="tool_wait:x:tool:0:1")

    deps = _deps(fake_storage_provider, io, fake_event_bus, _ToolWaitExecutor(), claim_engine=_Claims())
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            with caplog.at_level(logging.WARNING):
                outcome = await run_one_session_turn(_make_lease(sid), deps)
    finally:
        io.release.set()

    assert io.hung_calls >= 1, "the turn never met the dead workspace: the test is not in its situation"
    assert outcome.park is not None, "the tool_wait park did not land (its release dropped the lease instead)"
    assert "did not accept" in " | ".join(r.getMessage() for r in caplog.records)


async def test_a_claims_turn_fails_instead_of_hanging_when_the_workspace_is_dead_before_the_tool_call_can_be_made_durable(
    fake_event_bus, fake_storage_provider, bounded_writes,
) -> None:
    """A flush that MUST be durable (the tool_call record, before a claim worker can be armed to read it from another process) does
    not carry on without it: the turn fails, with the lease released."""
    io = _Workspace()
    io.hung = True
    sid = "s-claims-dead"
    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=dispatch._now(), turn_status="running",
    ))

    class _ClaimsExecutor:
        _tool_calls_as_claims_enabled = True

        async def invoke(self, messages, **kwargs):
            yield ToolCallStart(id="call_a", name="tool_a", index=0)
            yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
            await asyncio.sleep(0)

    deps = _deps(fake_storage_provider, io, fake_event_bus, _ClaimsExecutor(), claim_engine=_Claims())
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            outcome = await run_one_session_turn(_make_lease(sid), deps)
    finally:
        io.release.set()

    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "failed"
    assert outcome.drop_lease


async def test_a_stopped_turn_fails_its_later_writes_at_once_once_the_writer_has_given_up(
    seeded_session, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
) -> None:
    """The cancelled exit waits for the writer three times (the output streamed before the Stop, the CANCELLED record, ...), each
    bounded on its own (5 s and 10 s), and each for the SAME dead batch. With the writer's bound SHORTER than those (here 0.2 s;
    the default is longer, so with it the exit keeps its own bounds as before) the first wait breaks the writer and the rest fail
    at once, so the exit lands at the writer's bound."""
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", WRITE_BOUND_S, raising=False)
    sid = seeded_session.id
    io = _Workspace()
    published = _Published(fake_event_bus)

    async def workspace_dies_then_stop() -> None:
        await asyncio.sleep(0.2)               # the tool_call record has sat in the writer's buffer past its age limit
        io.hung = True
        await _request_stop(fake_storage_provider, fake_event_bus, sid)
        await asyncio.sleep(0.1)

    script = [
        TextDelta(text="first", index=0),
        ToolCallStart(id="t1", name="x", index=0), ToolCallEnd(id="t1", arguments={}, index=0),
        TextDelta(text="the part streamed when the stop landed", index=1),
        workspace_dies_then_stop, "BLOCK",
    ]
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=io, event_bus=fake_event_bus,
        build_executor=_build_returning(_StopAwareExecutor(script)),
    )
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            with caplog.at_level(logging.WARNING):
                outcome = await run_one_session_turn(_make_lease(sid), deps)
    finally:
        io.release.set()

    assert io.hung_calls >= 1, "the stop never met the dead workspace: the test is not in its situation"
    assert outcome.success and outcome.drop_lease
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row.status == SessionStatus.WAITING and row.ended_reason is None
    assert f"session:{sid}:terminal" in published.keys
