"""Graph agent node that calls a yielding tool is captured (parked), not failed."""
from collections.abc import AsyncIterator

import pytest

from primer.graph.base import _PendingAgentYield
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import StreamEvent
from primer.model.graph import (
    Graph, GraphNodeMessage, GraphThread,
    _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


class _YieldingLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:t1:tc1",
                        resume_metadata={"prompt": "color?"}),
                tool_call_id="tc1",
                llm_messages=[{"role": "assistant", "parts": []}])
            yield  # pragma: no cover
        return _g()


async def _drain_until_yield(it):
    evs = []
    try:
        async for ev in it:
            evs.append(ev)
    except YieldToWorker as exc:
        return evs, exc
    return evs, None


def _agent():
    return Agent(id="x", description="x",
                 model=AgentModel(provider_id="p", model_name="m"))


def _model():
    return LLMModel(name="m", context_length=128_000)


async def _mk_executor(graph, llm):
    async def agent_resolver(_):
        return _agent()

    async def llm_resolver(_):
        return (llm, _model())

    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)  # type: ignore[arg-type]
    return GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=ts, message_storage=ms,  # type: ignore[arg-type]
        graph_thread_id=thread.id)


def _graph():
    return Graph(id="g", description="b->A->e", nodes=[
        _BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x"),
        _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="A"),
               _StaticEdge(from_node="A", to_node="exit")])


@pytest.mark.asyncio
async def test_agent_node_yield_is_captured_not_failed():
    ex = await _mk_executor(_graph(), _YieldingLLM())
    _evs, raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None, "agent-node yield must propagate (park), not fail"
    assert len(ex._pending_agent_yields) == 1
    p = ex._pending_agent_yields[0]
    assert p.node_id == "A" and p.tool_call_id == "tc1"
    assert p.event_key == "ask_user:t1:tc1" and p.tool_name == "ask_user"
    assert p.resume_metadata.get("prompt") == "color?"
    assert p.llm_messages == [{"role": "assistant", "parts": []}]


@pytest.mark.asyncio
async def test_pending_agent_yields_roundtrip_snapshot():
    ex = await _mk_executor(_graph(), _YieldingLLM())
    ex._pending_agent_yields = [_PendingAgentYield(
        node_id="A", tool_call_id="tc1", event_key="ask_user:t:tc1",
        tool_name="ask_user", resume_metadata={"prompt": "q"},
        llm_messages=[{"role": "assistant"}], iteration=1)]
    import json
    payload = ex.snapshot_state()
    json.dumps(payload)
    ex2 = await _mk_executor(_graph(), _YieldingLLM())
    ex2.restore_state(payload)
    assert len(ex2._pending_agent_yields) == 1
    p = ex2._pending_agent_yields[0]
    assert p.node_id == "A" and p.event_key == "ask_user:t:tc1"
    assert p.tool_name == "ask_user" and p.iteration == 1
    assert p.llm_messages == [{"role": "assistant"}]
