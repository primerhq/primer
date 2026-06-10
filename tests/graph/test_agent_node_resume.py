"""Executor-side resume of a parked agent node: rebuild + continue its turn."""
from collections.abc import AsyncIterator

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, Message, StreamEvent, TextDelta, TextPart, ToolResultPart,
)
from primer.model.graph import (
    Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_workspace_executor import _make_state_repo


def _agent():
    return Agent(id="x", description="x",
                 model=AgentModel(provider_id="p", model_name="m"),
                 system_prompt=["Be terse."])


class _YieldingLLM:
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:t:tc1",
                        resume_metadata={"prompt": "color?"}),
                tool_call_id="tc1",
                llm_messages=[Message(role="assistant",
                                      parts=[TextPart(text="(calling ask_user)")]).model_dump(mode="json")])
            yield  # pragma: no cover
        return _g()


class _ContinuationLLM:
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            yield TextDelta(text="Noted: blue.", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


def _graph():
    return Graph(id="g", description="b->A->e", nodes=[
        _BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x"),
        _EndNode(id="exit", output_template="{{ nodes.A.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="A"),
               _StaticEdge(from_node="A", to_node="exit")])


async def _build(tmp_path, llm, gsid):
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(_): return _agent()
    async def llm_resolver(_): return (llm, LLMModel(name="m", context_length=128_000))

    return WorkspaceGraphExecutor(
        graph=_graph(), agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo, graph_session_id=gsid)


async def _drain_until_yield(it):
    try:
        async for _ev in it: pass
    except YieldToWorker as exc:
        return exc
    return None


@pytest.mark.asyncio
async def test_agent_node_park_then_resume_completes(tmp_path):
    # 1. Run to the agent-node yield; capture the checkpoint.
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid-r1")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    # 2. Fresh executor (as the worker builds) resumes with the answer.
    ex2 = await _build(tmp_path, _ContinuationLLM(), "gsid-r1")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="blue")])
    async for _ev in ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result):
        pass

    state = await ex2.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"
    assert state["node_states"]["A"]["status"] == "ended"
