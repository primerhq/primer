"""A failed node must never end its run 'completed'.

Observed live: an agent node whose LLM call failed exited status=failed,
the End node was never reached, and the session still reported
ended_reason=completed. Anything polling status - including the shell -
showed a failed graph run as a successful one.

The harness mirrors test_executor_error_emission: in-memory storages, a
resolver that fails the same way a dead provider does.
"""
from __future__ import annotations

import pytest

from primer.model.graph import Graph, GraphThread
from primer.model.workspace_session import SessionStatus

from tests.graph.test_executor_error_emission import (
    _build_executor,
    _drain,
)


def _one_agent_graph() -> Graph:
    return Graph(
        id="g-fail",
        description="begin -> agent -> end",
        nodes=[
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "work", "agent_id": "agent-x",
             "input_template": "do the thing"},
            {"kind": "end", "id": "finish"},
        ],
        edges=[
            {"kind": "static", "from_node": "start", "to_node": "work"},
            {"kind": "static", "from_node": "work", "to_node": "finish"},
        ],
    )


@pytest.mark.asyncio
async def test_failed_agent_node_ends_the_run_failed() -> None:
    executor, thread, thread_storage = await _build_executor(
        graph=_one_agent_graph(),
    )

    events = await _drain(executor.invoke([]))

    # The node's failure is visible in the stream...
    transitions = [
        (e.node_id, e.phase, e.status)
        for e in events
        if type(e).__name__ == "_GraphTransitionEvent"
    ]
    assert ("work", "exit", "failed") in transitions, transitions

    # ...and, load-bearingly, in the terminal state. 'completed' here is
    # the silent-success defect: the End node never ran.
    saved = await thread_storage.get(thread.id)
    assert saved.status == SessionStatus.ENDED
    assert saved.ended_reason == "failed", (
        f"a run whose node failed reported {saved.ended_reason!r}"
    )
