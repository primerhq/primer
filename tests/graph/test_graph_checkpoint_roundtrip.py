"""Phase 6 Task 6.1 — round-trip executor checkpoint state.

The executor's ``snapshot_state`` / ``restore_state`` pair must capture
enough internal state (GraphContext, ready set, fan-out bookkeeping,
pending ToolCalls) that a fresh executor can pick up where the prior
one left off, mid-graph.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.base import (
    _BaseGraphExecutor,
    _FanoutDrainState,
    _FanoutInstance,
    _PendingToolCall,
)
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent
from primer.model.chat import Message
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphContext,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
    _ToolCallNode,
)

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


@pytest.mark.asyncio
async def test_snapshot_and_restore_preserves_state() -> None:
    """Construct an executor, populate its internal state, snapshot, restore.

    The restored executor's attrs should equal the originals.
    """
    graph = Graph(
        id="g-cp",
        description="checkpoint round-trip",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fo",
                specs=[
                    FanOutSpec(
                        kind="broadcast",
                        target_node_id="worker",
                        count=2,
                        on_failure="drain_then_fail",
                    ),
                ],
            ),
            _ToolCallNode(
                id="worker",
                tool_id="web__search",
                arguments={"q": "x"},
            ),
            _FanInNode(id="fi", aggregate_template="{{ nodes.worker | length }}"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fo"),
            _StaticEdge(from_node="fo", to_node="worker"),
            _StaticEdge(from_node="worker", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="end"),
        ],
    )

    async def agent_resolver(agent_id: str) -> Agent:
        raise KeyError(agent_id)

    async def llm_resolver(agent):  # pragma: no cover -- not used
        raise NotImplementedError

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
    )

    # Synthesize state mid-execution.
    context = GraphContext(
        initial_input="seed",
        iteration=3,
        nodes={
            "begin": NodeOutput(text="seed", parsed=None, history=[], iteration=0),
            "fo": NodeOutput(
                text='{"node_id":"fo","specs":1}',
                parsed=None,
                history=[],
                iteration=1,
            ),
            "worker": [
                NodeOutput(text="r0", parsed=None, history=[], iteration=2),
            ],
        },
    )
    executor._context = context  # populate via setter convention
    executor._ready_set = {"worker[1]"}

    spec = graph.nodes[1].specs[0]  # type: ignore[union-attr]
    executor._fanout_instances = {
        "worker[0]": _FanoutInstance(
            synthesized_id="worker[0]", target_node_id="worker",
            fanout_index=0, fanout_item=context.nodes["fo"],
        ),
        "worker[1]": _FanoutInstance(
            synthesized_id="worker[1]", target_node_id="worker",
            fanout_index=1, fanout_item=context.nodes["fo"],
        ),
    }
    executor._fanout_target_expected_count = {"worker": 2}
    executor._instance_to_spec = {
        "worker[0]": ("fo", spec),
        "worker[1]": ("fo", spec),
    }
    executor._fanout_drain_state = {
        "fo__worker": _FanoutDrainState(
            on_failure="drain_then_fail",
            fanout_node_id="fo",
            target_node_id="worker",
            expected_count=2,
            completed_count=1,
        ),
    }
    executor._pending_toolcalls = [
        _PendingToolCall(
            node_id="worker[1]",
            tool_call_id="tc-abc",
            parked_event_key="tool_approval:gsid-1:tc-abc",
            arguments={"q": "x", "limit": 10},
        ),
    ]

    payload = executor.snapshot_state()
    # JSON-friendly: should round-trip via json.dumps without TypeError.
    import json

    json.dumps(payload)

    # Restore into a second executor.
    thread2 = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor2 = GraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread2.id,
    )
    executor2.restore_state(payload)

    assert executor2._context.initial_input == "seed"
    assert executor2._context.iteration == 3
    assert set(executor2._context.nodes.keys()) == {"begin", "fo", "worker"}
    worker_entry = executor2._context.nodes["worker"]
    assert isinstance(worker_entry, list)
    assert len(worker_entry) == 1
    assert worker_entry[0].text == "r0"

    assert executor2._ready_set == {"worker[1]"}

    assert set(executor2._fanout_instances.keys()) == {"worker[0]", "worker[1]"}
    assert executor2._fanout_instances["worker[0]"].fanout_index == 0
    assert executor2._fanout_instances["worker[1]"].fanout_index == 1
    assert executor2._fanout_target_expected_count == {"worker": 2}

    assert set(executor2._instance_to_spec.keys()) == {"worker[0]", "worker[1]"}
    fanout_id, restored_spec = executor2._instance_to_spec["worker[0]"]
    assert fanout_id == "fo"
    assert restored_spec.kind == "broadcast"
    assert restored_spec.target_node_id == "worker"
    assert restored_spec.count == 2
    assert restored_spec.on_failure == "drain_then_fail"

    assert "fo__worker" in executor2._fanout_drain_state
    drain = executor2._fanout_drain_state["fo__worker"]
    assert drain.expected_count == 2
    assert drain.completed_count == 1
    assert drain.on_failure == "drain_then_fail"

    assert len(executor2._pending_toolcalls) == 1
    p = executor2._pending_toolcalls[0]
    assert p.node_id == "worker[1]"
    assert p.tool_call_id == "tc-abc"
    assert p.parked_event_key == "tool_approval:gsid-1:tc-abc"
    assert p.arguments == {"q": "x", "limit": 10}
