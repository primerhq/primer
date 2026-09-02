"""01a05935 item 3: fan-out agent-node siblings must not share turn history.

_stream_agent_node's live-dispatch path is covered end-to-end by
tests.graph.test_spec_b_end_to_end (3 concurrent broadcast workers, each
persists under its own instance-qualified node_id). This file covers the
OTHER call path with the same bug: _resume_agent_node, continuing a
parked fan-out instance's turn after an ask_user/approval answer arrives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.graph._node_refs import _FanoutInstance, _PendingAgentYield
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, Message, StreamEvent, TextDelta, TextPart, ToolResultPart,
)
from primer.model.graph import (
    FanOutSpec,
    Graph,
    GraphContext,
    GraphNodeMessage,
    GraphThread,
    NodeOutput,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _FanInNode,
    _FanOutNode,
    _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


class _ContinuationLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, **kw: Any) -> AsyncIterator[StreamEvent]:
        async def _g():
            yield TextDelta(text="continued", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


def _graph() -> Graph:
    return Graph(
        id="g-resume-fanout",
        description="begin -> fan_out -> worker(agent) -> fan_in -> end",
        nodes=[
            _BeginNode(id="begin"),
            _FanOutNode(
                id="fo",
                specs=[FanOutSpec(
                    kind="broadcast", target_node_id="worker", count=2,
                )],
            ),
            _AgentNodeRef(id="worker", agent_id="ag", input_template="continue"),
            _FanInNode(id="fi", aggregate_template="{{ nodes.worker | length }}"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="fo"),
            _StaticEdge(from_node="worker", to_node="fi"),
            _StaticEdge(from_node="fi", to_node="end"),
        ],
    )


@pytest.mark.asyncio
async def test_resume_agent_node_persists_history_under_the_instance_id() -> None:
    """A parked fan-out agent instance ("worker[1]") resuming its turn
    must load/persist history under "worker[1]", not the shared base id
    "worker" that _resolve_node_def collapses pending.node_id to when
    looking up the node DEFINITION (a different, correct use of that
    collapse - the definition is shared, the history must not be)."""
    graph = _graph()

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id=agent_id, description="x",
                     model=AgentModel(profile_id="p--m"), system_prompt=[])

    llm = _ContinuationLLM()

    async def llm_resolver(agent: Agent):
        return (llm, ResolvedModel(
            profile_id="test-profile", provider_id="test-provider",
            model_name="m", context_length=128_000,
            config=ModelProfileConfig(),
        ))

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=thread_storage,  # type: ignore[arg-type]
        message_storage=message_storage,  # type: ignore[arg-type]
        graph_thread_id=thread.id,
    )

    # Sibling worker[0] already finished and left history under its own
    # id - proves worker[1]'s resume doesn't read or write into it.
    await executor._persist_node_turn(
        "worker[0]", 0,
        [Message(role="user", parts=[TextPart(text="sibling 0's turn")])],
    )

    executor._context = GraphContext(
        initial_input="seed", iteration=1,
        nodes={"begin": NodeOutput(text="seed", parsed=None, history=[], iteration=0)},
    )
    executor._fanout_instances = {
        "worker[1]": _FanoutInstance(
            synthesized_id="worker[1]", target_node_id="worker",
            fanout_index=1, fanout_item=None,
        ),
    }
    pending = _PendingAgentYield(
        node_id="worker[1]",
        tool_call_id="tc-1",
        event_key="ask_user:gsid:tc-1",
        tool_name="ask_user",
        resume_metadata={"prompt": "continue?"},
        llm_messages=[
            Message(role="assistant", parts=[TextPart(text="(asking)")])
            .model_dump(mode="json"),
        ],
        iteration=1,
    )
    tool_result_msg = Message(
        role="tool", parts=[ToolResultPart(id="tc-1", output="yes")],
    )

    await executor._resume_agent_node(pending, tool_result_msg)

    persisted_node_ids = {m.node_id for m in message_storage._data.values()}
    assert persisted_node_ids == {"worker[0]", "worker[1]"}, (
        f"worker[1]'s resume must persist under its own instance id, "
        f"not merge into worker[0]'s or the shared base 'worker' - "
        f"got {sorted(persisted_node_ids)}"
    )

    sibling_rows = [
        m for m in message_storage._data.values() if m.node_id == "worker[0]"
    ]
    assert len(sibling_rows) == 1, "worker[1]'s resume must not touch worker[0]'s history"
    assert sibling_rows[0].parts[0].text == "sibling 0's turn"  # type: ignore[union-attr]

    worker1_rows = [
        m for m in message_storage._data.values() if m.node_id == "worker[1]"
    ]
    assert len(worker1_rows) >= 1
