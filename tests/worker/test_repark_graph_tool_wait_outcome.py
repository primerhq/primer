"""``repark_graph_outcome`` / ``_repark_graph_tool_wait_outcome`` (Phase 3
stage 7a, 01a0518b boundary d) - direct unit tests for the ToolWaitPark-
shaped repark builder. Exercised end-to-end by the graph order tests,
but its own output shape (per-node wake keys, node_tool_call_seq
threading, notifying-result inclusion) had no direct test pinning it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.chat import ToolResultPart
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)
from primer.model.yield_ import ToolWaitPark
from primer.worker.graph_resume_coordinator import repark_graph_outcome
from primer.worker.yield_runtime import ToolWaitParkedState


def _session(turn_no: int = 0) -> WorkspaceSession:
    return WorkspaceSession(
        id="gs-1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc),
        turn_no=turn_no,
    )


def test_dispatches_to_tool_wait_builder_for_toolwaitpark() -> None:
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.success is True
    assert outcome.drop_lease is True
    assert outcome.park is not None
    assert outcome.park.parked_event_key == "tool_wait:gs-1:0:A"
    assert outcome.park.parked_event_keys == ["tool_wait:gs-1:0:A"]
    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.outstanding_task_ids == ["A:tool:0:1"]
    assert parked_state.graph_checkpoint == repark.graph_checkpoint


def test_multi_node_pending_produces_multi_event_keys() -> None:
    """Two co-pending nodes each get their OWN wake key in
    parked_event_keys - proves the multi-event pure-park shape survives
    a repark, not just the original park."""
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1", "B:tool:0:1"],
        event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},
            {"node_id": "B", "outstanding_task_ids": ["B:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.park.parked_event_keys == [
        "tool_wait:gs-1:0:A", "tool_wait:gs-1:0:B",
    ]


def test_node_tool_call_seq_is_threaded_through() -> None:
    session = _session()
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {"node_id": "A", "outstanding_task_ids": ["A:tool:0:1"], "notifying_results": []},
        ],
    }

    outcome = repark_graph_outcome(None, session, repark, node_tool_call_seq={"A": 3})

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.node_tool_call_seq == {"A": 3}


def test_notifying_results_included_in_task_ids() -> None:
    session = _session()
    result = ToolResultPart(id="A:tool:0:2", output="inline", error=False)
    repark = ToolWaitPark(
        outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        notifying_results=[("A:tool:0:2", result)],
    )
    repark.graph_checkpoint = {
        "pending_tool_waits": [
            {
                "node_id": "A", "outstanding_task_ids": ["A:tool:0:1"],
                "notifying_results": [
                    ("A:tool:0:2", {"id": "A:tool:0:2", "output": "inline", "error": False}),
                ],
            },
        ],
    }

    outcome = repark_graph_outcome(None, session, repark)

    parked_state = ToolWaitParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.notifying_task_ids == ["A:tool:0:2"]


def test_dispatches_to_yield_builder_for_yieldtoworker() -> None:
    """The dispatcher's OTHER branch - a co-pending human gate still
    produces the classic ParkedState/Yielded shape, not the tool_wait
    one, proving isinstance() picks the right builder both ways."""
    from primer.model.yield_ import Yielded, YieldToWorker
    from primer.worker.yield_runtime import ParkedState

    session = _session()
    repark = YieldToWorker(
        Yielded(tool_name="_approval", event_key="ask_user:B:tc-b"),
        tool_call_id="tc-b",
    )
    repark.graph_checkpoint = {"pending_agent_yields": [{"node_id": "B", "tool_call_id": "tc-b"}]}

    outcome = repark_graph_outcome(None, session, repark)

    assert outcome.park.parked_event_key == "ask_user:B:tc-b"
    parked_state = ParkedState.from_jsonable(outcome.park.parked_state)
    assert parked_state.tool_call_id == "tc-b"
