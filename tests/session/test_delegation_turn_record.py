"""``DelegationRecorder.append_turn_record``: a record of the turn's own, through the turn's writer (pivot P5, Task 6).

The agent applying a canvas batch from a tool call is not a subagent run, but it runs INSIDE the turn and owns no writer, so it
reaches the turn's through the recorder the dispatch publishes on a contextvar. The record takes the turn's next seq and carries no
delegation stamp.
"""

from __future__ import annotations

import asyncio

from primer.agent.call_scope import CallScope, bind_call_scope
from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
from primer.model.workspace_session import SessionMessageKind
from primer.session.delegation import DelegationRecorder


class _Writer:
    def __init__(self) -> None:
        self.records: list = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records) + 40               # the turn had numbered 40 records already


class _Bus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, key, payload) -> None:
        self.published.append((key, payload))


class _DownBus:
    async def publish(self, key, payload) -> None:
        raise RuntimeError("bus down")


async def test_append_turn_record_uses_the_turns_writer_and_ticks():
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    seq = await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1, "source": "agent"})
    assert seq == 41
    (record,) = w.records
    assert record.kind is SessionMessageKind.CANVAS_OP and record.payload == {"rev": 1, "source": "agent"}
    assert record.node_id is None and record.created_at is not None
    assert b.published == [("session:s:tick", {"seq": 41})]


async def test_the_record_is_the_turns_own_and_carries_no_delegation_stamp():
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")
    await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1})
    (record,) = w.records
    assert not {key for key in record.payload if key.startswith("delegate") or key == "delegated"}


async def test_the_recorder_says_which_session_it_writes_for():
    assert DelegationRecorder(writer=_Writer(), event_bus=_Bus(), session_id="ses-7").session_id == "ses-7"


async def test_the_node_of_a_graph_turn_rides_the_record():
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")
    entered = set_current_graph_node_id("worker[0]")
    try:
        await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1})
    finally:
        reset_current_graph_node_id(entered)
    assert w.records[0].node_id == "worker[0]"


async def test_an_abandoned_call_writes_nothing():
    """Stop slice B1: what a call the Stop gave up on still emits must not land after the call's "interrupted" answer."""
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    scope = CallScope()
    scope.abandon()
    results: list = []

    async def work() -> None:
        bind_call_scope(scope)
        results.append(await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1}))

    await asyncio.create_task(work())
    assert results == [None] and w.records == [] and b.published == []


async def test_a_call_that_is_not_abandoned_still_writes():
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")
    results: list = []

    async def work() -> None:
        bind_call_scope(CallScope())
        results.append(await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1}))

    await asyncio.create_task(work())
    assert results == [41] and len(w.records) == 1


async def test_a_tick_that_cannot_be_published_does_not_lose_the_seq():
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_DownBus(), session_id="s")
    assert await rec.append_turn_record(SessionMessageKind.CANVAS_OP, {"rev": 1}) == 41
    assert len(w.records) == 1


async def test_a_turn_without_a_bus_still_records():
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=None, session_id="s")
    assert await rec.append_turn_record(SessionMessageKind.CANVAS_EVENT, {"id": "e1"}) == 41
    assert w.records[0].kind is SessionMessageKind.CANVAS_EVENT
