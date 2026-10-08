"""A workspace that never answers must not hold a turn's exit at its last flush (ticket 01a11b58).

``_flush_and_tick`` made the turn's last records durable with ``writer.flush()``, which waits without limit for the batch the writer
keeps in flight. On the clean-completion exit, the failure exit and both park exits that wait held the lease release and the
session's lifecycle (the cancelled exit has bounds of its own, but each of them waited the full time for the same dead batch, one
after the other).

Here the workspace stops answering and every exit is run through ``run_one_session_turn``: it must land, within the bound, with the loss
logged and the tick published. Every test body is bounded: no path may hang the lane.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch  # noqa: F401  (patched by name in the stopped case)
from primer.model.chat import Done, TextDelta, ToolCallEnd, ToolCallStart
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
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
WRITE_BOUND_S = 0.2    # the writer's bound


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


class _Workspace(FakeWorkspaceIO):
    """A workspace whose runtime connection can drop: while ``hung`` every append waits for ``release``."""

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


@pytest.fixture
def bounded_writes(monkeypatch):
    # raising=False: before the bound exists the setting is inert and the tests fail by hanging, which the body bound reports
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", WRITE_BOUND_S, raising=False)


async def _boom() -> None:
    raise RuntimeError("the model call blew up")


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


def _deps(storage, io, bus, executor) -> SessionDispatchDeps:
    return SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=_build_returning(executor),
    )


@pytest.mark.parametrize("exit_kind", ["done", "failed", "parked"])
async def test_a_turn_exit_lands_when_the_workspace_never_answers_its_last_flush(
    seeded_session, fake_event_bus, fake_storage_provider, bounded_writes, caplog, exit_kind,
) -> None:
    sid = seeded_session.id
    io = _Workspace()
    io.hung = True                       # the connection dropped before the turn's records were flushed
    published = _Published(fake_event_bus)
    script = {
        "done": [TextDelta(text="hello", index=0), Done(stop_reason="stop", raw_reason="stop")],
        "failed": [TextDelta(text="hello", index=0), _boom],
        "parked": [TextDelta(text="hello", index=0), _park(sid)],
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
    if exit_kind == "parked":
        assert outcome.park is not None, "the turn did not park"
    elif exit_kind == "failed":
        assert row.status == SessionStatus.ENDED and row.ended_reason == "failed"
    else:
        assert outcome.success and row.status != SessionStatus.RUNNING
    assert f"session:{sid}:tick" in published.keys, "the exit never published the tick that follows its flush"
    text = " | ".join(r.getMessage() for r in caplog.records)
    assert "did not accept" in text, f"the lost records were not logged: {text}"


async def test_a_stopped_turn_lands_at_the_writers_bound_not_after_each_best_effort_bound_in_turn(
    seeded_session, fake_event_bus, fake_storage_provider, bounded_writes, caplog,
) -> None:
    """The cancelled exit waits for the writer three times (the output streamed before the Stop, the CANCELLED record, the turn log's
    flushes), each bounded on its own (5 s and 10 s), and each of them for the SAME dead batch. Once the writer gives up on it, the rest
    fail at once and the exit lands at the writer's bound."""
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
    deps = _deps(fake_storage_provider, io, fake_event_bus, _StopAwareExecutor(script))
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
