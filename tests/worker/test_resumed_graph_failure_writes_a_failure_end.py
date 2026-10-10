"""A graph that parks and then FAILS inside its resume gets a failure end, through REAL executors (ticket 01a11f35, round 4 of #701, review B2-N3).

``end_graph`` writes the record that closes the graph's turn from ``graph_end_for(executor.last_done_reason, executor)``. The failure exits of ``resume_from_checkpoint`` (a rejected approval, a resumed agent
node or tool_wait node that fails) saved ``ENDED/failed`` but never set the executor's outcome, so the record said ``done(stop, graph_ended, completed)``: the log claimed a clean end for a failed graph, and
the turn window read as completed. The round 3 tests of the coordinators used ``SimpleNamespace`` stand-ins for the executor and could not see it; every test here builds the real executor, parks it, resumes
a fresh one through ``resume_graph_engine`` or ``resume_graph_tool_wait`` and reads the record in the log: ``done(error, graph_failed, failed)``, node-less, with the graph's own end.
(The label the coordinator returns, and the reason the pool ends the row with, are the known ticket: they say "completed" for a drained resume whatever its outcome; the RECORD is what this pins.)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.graph import GraphNodeMessage, GraphThread
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.worker import graph_resume_coordinator
from primer.worker.tool_wait_resume_coordinator import resume_graph_tool_wait
from primer.worker.yield_runtime import ParkedState, ToolWaitParkedState
from tests._resume_hook_fakes import EngineFakePool, EngineStorageProvider
from tests.graph.test_resume_failure_exits_expose_the_outcome import _fail_when_resumed, _toolcall_graph
from tests.graph.test_tool_wait_graph_park import _mk_parallel_executor, _tool_wait_park
from tests.graph.test_toolcall_dispatch import _InMemoryStorage
from tests.session.graph_turn_paths import FAIL, SID, TurnLogs, open_turn, park, records
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider, fake_workspace_io  # noqa: F401  (fixtures)
from tests.worker.test_graph_resume_writes_the_graph_end import _records, _world

pytestmark = pytest.mark.asyncio


def _graph_ends(recs: list[dict]) -> list[dict]:
    return [r for r in recs if r["kind"] == "done" and not r.get("node_id") and (r.get("payload") or {}).get("graph_end")]


def _is_the_failure_end(recs: list[dict]) -> None:
    ends = _graph_ends(recs)
    assert len(ends) == 1, f"exactly one graph end expected, got {[r['payload'] for r in ends]}"
    assert ends[0]["payload"] == {"stop_reason": "error", "raw_reason": "graph_failed", "graph_end": True, "ended_reason": "failed"}, ends[0]["payload"]


async def test_a_resumed_agent_node_whose_model_call_fails_gets_a_failure_end(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The real WorkspaceGraphExecutor and the real dispatch park the graph at an ask_user; the resumed worker's model call then fails."""
    tl = TurnLogs()
    await open_turn(fake_storage_provider, fake_workspace_io)
    ask, parked, executor = await park(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, tl, monkeypatch, scripts=FAIL)
    monkeypatch.undo()
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = (await sessions.get(SID)).model_copy(update={
        "status": SessionStatus.WAITING, "parked_status": "resumable", "parked_state": {"resume_event_key": ask.yielded.event_key}, "parked_at": datetime.now(UTC),
    })
    await sessions.update(row)
    blob = {**parked.park.parked_state, "resume_event_payload": {"response": "blue"}}
    pool = EngineFakePool(storage=fake_storage_provider, workspace_io=fake_workspace_io, executor_factory=executor)

    await graph_resume_coordinator.resume_graph_engine(pool, row, ParkedState.from_jsonable(blob))

    _is_the_failure_end(records(fake_workspace_io))


async def test_a_rejected_approval_gets_a_failure_end() -> None:
    graph = _toolcall_graph()
    gate = Yielded(tool_name="_approval", event_key="tool_approval:gs-1:tc-1")

    async def first(node, arguments):
        raise YieldToWorker(gate, tool_call_id="tc-1")

    async def agent_resolver(agent_id):
        raise KeyError(agent_id)

    async def llm_resolver(agent):
        raise NotImplementedError

    ts, ms = _InMemoryStorage(GraphThread), _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)

    def build(dispatcher):
        return GraphExecutor(graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver, thread_storage=ts, message_storage=ms, graph_thread_id=thread.id, tool_dispatcher=dispatcher)

    with pytest.raises(YieldToWorker) as parked:
        async for _ in build(first).invoke([]):
            pass
    storage, io, session = await _world(parked_state={"resume_event_key": gate.event_key})

    async def executor():
        return build(first)          # the coordinator turns a rejected decision into _ToolApprovalRejected; the dispatcher is not used

    state = ParkedState(
        yielded=gate, llm_messages=[], turn_no=0, started_at=datetime.now(UTC), tool_call_id="tc-1",
        resume_event_payload={"decision": "rejected", "reason": "no"}, graph_checkpoint=parked.value.graph_checkpoint,
    )
    pool = EngineFakePool(storage=storage, workspace_io=io, executor_factory=executor)

    await graph_resume_coordinator.resume_graph_engine(pool, session, state)

    _is_the_failure_end(_records(io))


async def test_a_resumed_tool_wait_node_that_fails_gets_a_failure_end(monkeypatch) -> None:
    ex = await _mk_parallel_executor()
    _fail_when_resumed(monkeypatch, {"agent-a": _tool_wait_park("A", "1")})
    with pytest.raises(ToolWaitPark) as parked:
        async for _ in ex.invoke([]):
            pass
    storage, io, session = await _world()
    await storage.get_storage(ToolCallTask).create(ToolCallTask(
        id="A:tool:0:1", session_id="gs-1", turn_no=0, tool_name="t", state=ToolCallTaskState.DONE, record_seq=1, created_at=datetime.now(UTC),
        result_state={"id": "A:tool:0:1", "output": "result A", "error": False},
    ))
    pool = EngineFakePool(storage=storage, workspace_io=io, executor_factory=_mk_parallel_executor)
    state = ToolWaitParkedState(
        outstanding_task_ids=["A:tool:0:1"], notifying_task_ids=[], event_key="tool_wait:A:tool:0:1", llm_messages=[], turn_no=0, started_at=datetime.now(UTC),
        graph_checkpoint=parked.value.graph_checkpoint,
    )

    await resume_graph_tool_wait(pool, session, state)

    _is_the_failure_end(_records(io))


async def test_a_resumed_graph_that_succeeds_still_gets_the_clean_end(tmp_path, monkeypatch, fake_workspace_io, fake_event_bus, fake_storage_provider) -> None:
    """The control: the same park and resume with a model call that succeeds writes ``done(stop, graph_ended, completed)``."""
    from tests.session.graph_turn_paths import OK

    tl = TurnLogs()
    await open_turn(fake_storage_provider, fake_workspace_io)
    ask, parked, executor = await park(tmp_path, fake_storage_provider, fake_workspace_io, fake_event_bus, tl, monkeypatch, scripts=OK)
    monkeypatch.undo()
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = (await sessions.get(SID)).model_copy(update={
        "status": SessionStatus.WAITING, "parked_status": "resumable", "parked_state": {"resume_event_key": ask.yielded.event_key}, "parked_at": datetime.now(UTC),
    })
    await sessions.update(row)
    blob = {**parked.park.parked_state, "resume_event_payload": {"response": "blue"}}
    pool = EngineFakePool(storage=fake_storage_provider, workspace_io=fake_workspace_io, executor_factory=executor)

    await graph_resume_coordinator.resume_graph_engine(pool, row, ParkedState.from_jsonable(blob))

    ends = _graph_ends(records(fake_workspace_io))
    assert len(ends) == 1 and ends[0]["payload"] == {"stop_reason": "stop", "raw_reason": "graph_ended", "graph_end": True, "ended_reason": "completed"}, [e["payload"] for e in ends]
