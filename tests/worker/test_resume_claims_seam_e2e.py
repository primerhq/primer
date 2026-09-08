"""The closing proof for 7a gate verdict item 3 (Phase 3 stage 7a,
01a0518b): a resumed node's OWN continuation dispatches a SECOND
claims batch through the REAL gate - no hand-raised ToolWaitPark, no
monkeypatched run_agent_turn.

Before item 3's fix, this was structurally impossible: no resume
coordinator ever bound coalesce_state onto the freshly-built resume
executor, so resolve_scoped_call was always None inside
_resume_agent_node, and run_agent_turn's own routing gate
(tool_calls_as_claims_enabled AND resolve_scoped_call is not None) was
always False on resume regardless of the flag. The tw_pending/ay_pending
except-ToolWaitPark arms in resume_from_checkpoint existed only to be
exercised by tests that hand-bind coalesce_state or monkeypatch
run_agent_turn directly - never by production code.

This test drives a REAL two-round FakeLLM script through a REAL
WorkspaceGraphExecutor + a REAL ToolExecutionManager (an empty toolset
provider, so every call is claimable): round 1 parks via a genuine
_dispatch_as_claims call (live dispatch, already working before this
arc); round 2 - triggered by resume_graph_tool_wait resuming the SAME
node's continuation after round 1's task goes terminal - must ALSO
route through _dispatch_as_claims for real, re-parking with a NEW
ToolCallTask row for the second batch, not silently falling through to
the classic in-process path or failing the node.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Done, ToolCallEnd, ToolCallStart
from primer.model.principal import PrincipalRef
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark
from primer.session.persistence import (
    WorkspaceMessageWriter,
    _CoalesceState,
    stash_and_flush_tool_call_record,
    stash_graph_scoped_ids,
    translate_stream_event,
)
from primer.worker.tool_wait_resume_coordinator import resume_graph_tool_wait
from primer.worker.yield_runtime import ToolWaitParkedState

from tests.conftest import _FakeStorageProvider
from tests.graph.test_workspace_executor import (
    _FakeLLM,
    _FakeToolsetProvider,
    _agent,
    _build_executor,
    _make_state_repo,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    async def append_message_line(self, session_id: str, line: bytes) -> None:
        return None


class _FakePool:
    def __init__(self, *, storage, workspace_io, executor_factory) -> None:
        self._storage = storage
        self._workspace_io = workspace_io
        self._event_bus = None
        self._executor_factory = executor_factory
        self.end_session_calls: list[str] = []
        self.repark_calls: list = []

    async def _load_workspace_for_persist(self, workspace_id: str):
        return self._workspace_io

    async def _build_graph_executor(self, session, workspace):
        return await self._executor_factory()

    async def _end_session(self, session, *, reason: str):
        self.end_session_calls.append(reason)
        return f"ENDED:{reason}"

    def _repark_graph_outcome(self, session, repark, *, node_tool_call_seq=None):
        self.repark_calls.append(repark)
        return "REPARKED"


@pytest.mark.asyncio
async def test_resumed_node_dispatches_second_claims_batch_through_the_real_gate(
    tmp_path,
) -> None:
    from primer.model.graph import (
        Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
    )

    graph = Graph(
        id="g-claims-seam", description="begin -> A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x"),
            _EndNode(id="exit"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )
    # Round 1 (live dispatch) and round 2 (the RESUMED node's own
    # continuation) each emit one claimable tool call - neither
    # notifying, both routed to the SAME empty-toolset provider so
    # is_notifying() is False for both and _dispatch_as_claims fires
    # both times.
    llm = _FakeLLM(scripts=[
        [
            ToolCallStart(id="call-1", name="fake__echo", index=0),
            ToolCallEnd(id="call-1", arguments={"round": 1}, index=0),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ],
        [
            ToolCallStart(id="call-2", name="fake__echo", index=0),
            ToolCallEnd(id="call-2", arguments={"round": 2}, index=0),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ],
    ])
    provider = _FakeToolsetProvider(tool_id="echo", output="echo-out")

    async def tool_mgr_resolver(agent) -> ToolExecutionManager:
        return ToolExecutionManager(
            toolset_providers={"fake": provider},
            initiated_by=PrincipalRef.system(),
        )

    repo = await _make_state_repo(tmp_path)

    async def _mk_executor():
        ex = await _build_executor(
            graph=graph, llm=llm, state_repo=repo, graph_session_id="gsid-claims-seam",
            agents={"x": _agent("x")}, tool_manager_resolver=tool_mgr_resolver,
            tool_calls_as_claims_enabled=True,
        )
        return ex

    ex = await _mk_executor()
    # Mirrors dispatch.py's own live-turn wiring (run_one_session_turn):
    # a fresh _CoalesceState bound post-construction (the executor exists
    # before the coalesce_state does in production too), and every
    # streamed event fed through translate_stream_event + the SAME
    # shared stash+flush helper item 3 extracted - resolve_scoped_call
    # needs BOTH scoped_call_ids (from translate_stream_event) AND
    # tool_call_record_seq (from the stash) populated before
    # _dispatch_as_claims ever calls it, exactly as the live turn loop
    # guarantees via its own per-event processing.
    live_coalesce_state = _CoalesceState()
    ex.bind_coalesce_state(live_coalesce_state)
    live_writer = WorkspaceMessageWriter(
        workspace_io=_FakeWorkspaceIO(), session_id="s-claims-seam", start_seq=0,
    )

    async def _drive(stream, coalesce_state, writer):
        async for ev in stream:
            result = translate_stream_event(ev, coalesce_state, turn_no=0)
            if result is None:
                continue
            for rec in (result if isinstance(result, list) else [result]):
                seq = await writer.append(rec)
                await stash_and_flush_tool_call_record(
                    rec, seq, coalesce_state=coalesce_state, writer=writer,
                    tool_calls_as_claims_enabled=True,
                )

    with pytest.raises(ToolWaitPark) as excinfo:
        await _drive(ex.invoke([]), live_coalesce_state, live_writer)
    first_park = excinfo.value
    assert first_park.graph_checkpoint is not None
    scoped_id_1 = first_park.outstanding_task_ids[0]
    assert scoped_id_1.startswith("A:tool:")

    # Materialize the first batch's ToolCallTask row exactly like
    # dispatch.py's own except-ToolWaitPark branch would, using the SAME
    # record_seq/tool_name the live coalesce_state's own stash captured -
    # proof round 1 went through the real gate for real, not a
    # hand-built park.
    record_seq_1 = live_coalesce_state.tool_call_record_seq[scoped_id_1]
    tool_name_1 = live_coalesce_state.tool_call_record_name[scoped_id_1]
    assert tool_name_1 == "fake__echo"

    # Mirrors dispatch.py's own park-catch (the ParkedState.node_tool_call_seq
    # docstring): snapshot the live coalesce_state's per-node mint counters
    # into the park BEFORE it goes out of scope, so round 2's fresh resume
    # tap seeds its own counter past round 1's mint instead of restarting at
    # 0 and re-minting the SAME scoped id for node A's second batch.
    node_tool_call_seq_1 = stash_graph_scoped_ids(
        first_park.graph_checkpoint, live_coalesce_state,
    )

    storage = _FakeStorageProvider()
    task_storage = storage.get_storage(ToolCallTask)
    session_storage = storage.get_storage(WorkspaceSession)
    await task_storage.create(ToolCallTask(
        id=scoped_id_1, session_id="s-claims-seam", turn_no=0,
        tool_name=tool_name_1, state=ToolCallTaskState.DONE,
        record_seq=record_seq_1, created_at=_now(), finished_at=_now(),
        result_state={"id": scoped_id_1, "output": "result 1", "error": False},
        batch_task_ids=[scoped_id_1],
    ))
    session = WorkspaceSession(
        id="s-claims-seam", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="x"),
        status=SessionStatus.WAITING, created_at=_now(), turn_no=0,
        parked_at=_now(),
    )
    await session_storage.create(session)

    parked = ToolWaitParkedState(
        outstanding_task_ids=[scoped_id_1], notifying_task_ids=[],
        event_key=first_park.event_key, llm_messages=[], turn_no=0,
        started_at=_now(), graph_checkpoint=first_park.graph_checkpoint,
        node_tool_call_seq=node_tool_call_seq_1,
    )
    pool = _FakePool(
        storage=storage, workspace_io=_FakeWorkspaceIO(), executor_factory=_mk_executor,
    )

    outcome = await resume_graph_tool_wait(pool, session, parked)

    # The whole point: round 2's own tool call must have gone through
    # the REAL _dispatch_as_claims gate (item 3's fix) and re-parked -
    # not silently completed via the classic in-process path (which
    # would mean resolve_scoped_call was still None) and not failed the
    # node (which would mean the tap's own stash+flush wiring was wrong).
    assert pool.end_session_calls == []
    assert len(pool.repark_calls) == 1
    second_park = pool.repark_calls[0]
    assert isinstance(second_park, ToolWaitPark)
    scoped_id_2 = second_park.outstanding_task_ids[0]
    assert scoped_id_2.startswith("A:tool:")
    assert scoped_id_2 != scoped_id_1

    # A REAL ToolCallTask row exists for round 2's batch, materialized by
    # dispatch.py-shaped row-creation logic reading THIS resume's own
    # tap-backed coalesce_state - not a phantom/never-persisted park.
    second_pending = second_park.graph_checkpoint["pending_tool_waits"]
    assert [pw["node_id"] for pw in second_pending] == ["A"]
    assert second_pending[0]["outstanding_task_ids"] == [scoped_id_2]

    # Only ONE real tool call ever executed in-process (round 2 never
    # fell through to the classic path either) - both rounds' calls were
    # claims-dispatched, never actually invoked.
    assert provider.calls == []
