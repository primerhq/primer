"""``resume_graph_engine`` closes the graph's turn with the graph's own end on EVERY path that ends the session (ticket 01a11f35, round 2 of #701).

A graph that parks and is resumed ends through the pool (``pool._end_session`` writes the row, no record), so without a record of its own the turn it finishes stays an open window. The completed
path is pinned through the real writers in ``tests/session/test_graph_turn_real_writers.py``; this file pins the paths that end the session ``failed``: a resumable row with no ``parked_at``,
an executor that cannot be built, a drain that raises, and a re-park the turn cannot take. Each must leave ONE failure end (a node-less ``done`` with ``stop_reason: error``) in the log, written
BEFORE the session is ended (ending can realize a queued steer that reopens it), and a write that fails must not keep the session from ending.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from primer.model.workspace_session import WorkspaceSession
from primer.model.yield_ import Yielded
from primer.session.persistence import TurnInvariantError
from primer.worker import graph_resume_coordinator
from primer.worker.yield_runtime import ParkedState
from tests._resume_hook_fakes import EngineFakePool, EngineStorageProvider, waiting_graph_session
from tests.session.test_dispatch import FakeWorkspaceIO

KEY = "ask_user:gs-1:worker:tc-1"


def _parked() -> ParkedState:
    return ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key=KEY, resume_metadata={"prompt": "?"}), llm_messages=[], turn_no=0, started_at=datetime.now(UTC),
        tool_call_id="tc-1", resume_event_payload={"response": "blue"}, graph_checkpoint={"pending_toolcalls": [], "pending_agent_yields": [], "pending_dispatch": []},
    )


async def _world(**session_updates):
    storage, io = EngineStorageProvider(), FakeWorkspaceIO()
    session = waiting_graph_session().model_copy(update={"parked_state": {"resume_event_key": KEY}, **session_updates})
    await storage.get_storage(WorkspaceSession).create(session)
    return storage, io, session


def _records(io: FakeWorkspaceIO) -> list[dict]:
    return [json.loads(line) for line in io.read_lines("gs-1")]


class _OrderRecordingPool(EngineFakePool):
    """Notes how many records the log held when the session was ended: the end must already be there."""

    def __init__(self, *args, io, **kwargs) -> None:
        super().__init__(*args, workspace_io=io, **kwargs)
        self._io = io
        self.records_when_ended: list[int] = []

    async def _end_session(self, session, *, reason: str):
        self.records_when_ended.append(len(self._io.read_lines("gs-1")))
        return await super()._end_session(session, reason=reason)


def _assert_one_failure_end(io: FakeWorkspaceIO, pool: _OrderRecordingPool) -> None:
    records = _records(io)
    assert [(r["kind"], r.get("node_id"), (r["payload"] or {}).get("stop_reason"), (r["payload"] or {}).get("graph_end")) for r in records] == [("done", None, "error", True)], records
    assert (records[0]["payload"] or {}).get("ended_reason") == "failed"
    assert pool.end_session_calls == ["failed"] and pool.records_when_ended == [1], "the end record is written BEFORE the session is ended"


async def _factory_that_raises():
    raise RuntimeError("cannot build")


@pytest.mark.asyncio
async def test_a_resumable_row_with_no_parked_at_ends_the_turn_failed() -> None:
    storage, io, session = await _world(parked_at=None)
    pool = _OrderRecordingPool(storage=storage, io=io, executor_factory=_factory_that_raises)

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    assert outcome == "ENDED:failed"
    _assert_one_failure_end(io, pool)


@pytest.mark.asyncio
async def test_an_executor_that_cannot_be_built_ends_the_turn_failed() -> None:
    storage, io, session = await _world()
    pool = _OrderRecordingPool(storage=storage, io=io, executor_factory=_factory_that_raises)

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    assert outcome == "ENDED:failed"
    _assert_one_failure_end(io, pool)


async def _executor():
    return SimpleNamespace()


@pytest.mark.asyncio
async def test_a_drain_that_raises_ends_the_turn_failed(monkeypatch) -> None:
    import primer.worker.graph_resume as graph_resume

    async def boom(**kwargs):
        raise RuntimeError("the drain failed")

    monkeypatch.setattr(graph_resume, "resume_graph_from_checkpoint", boom)
    storage, io, session = await _world()
    pool = _OrderRecordingPool(storage=storage, io=io, executor_factory=_executor)

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    assert outcome == "ENDED:failed"
    _assert_one_failure_end(io, pool)


@pytest.mark.asyncio
async def test_a_repark_the_turn_cannot_take_ends_the_turn_failed(monkeypatch) -> None:
    import primer.worker.graph_resume as graph_resume

    async def reparks(**kwargs):
        return ("approved", SimpleNamespace(graph_checkpoint={"pending_toolcalls": [], "pending_agent_yields": [], "pending_dispatch": []}), {})

    class _CannotRepark(_OrderRecordingPool):
        def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
            raise TurnInvariantError("a parked call has no durable record")

    monkeypatch.setattr(graph_resume, "resume_graph_from_checkpoint", reparks)
    storage, io, session = await _world()
    pool = _CannotRepark(storage=storage, io=io, executor_factory=_executor)

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    assert outcome == "ENDED:failed"
    _assert_one_failure_end(io, pool)


@pytest.mark.asyncio
async def test_the_row_carries_the_end_records_seq_so_the_next_writer_does_not_reuse_it() -> None:
    storage, io, session = await _world(parked_at=None)
    pool = _OrderRecordingPool(storage=storage, io=io, executor_factory=_factory_that_raises)

    await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    row = await storage.get_storage(WorkspaceSession).get("gs-1")
    assert row.last_seq == _records(io)[-1]["seq"] >= 1


@pytest.mark.asyncio
async def test_a_write_that_fails_does_not_keep_the_session_from_ending() -> None:
    class _BrokenIO(FakeWorkspaceIO):
        async def append_message_line(self, session_id: str, line: bytes) -> None:
            raise OSError("the workspace is gone")

    storage, _, session = await _world(parked_at=None)
    io = _BrokenIO()
    pool = _OrderRecordingPool(storage=storage, io=io, executor_factory=_factory_that_raises)

    outcome = await graph_resume_coordinator.resume_graph_engine(pool, session, _parked())

    assert outcome == "ENDED:failed" and pool.end_session_calls == ["failed"], "a lost end record has never been allowed to keep a session alive"
