"""Guard the Quickstart Step 6 claim: a graph agent node that calls
workspace_ext__watch_files is parked (not failed), and resumes to completion
when the watch-files tool result is delivered (modelling: file written ->
watch_files resume hook delivers the change as the tool result -> graph ends).

Modelled exactly on tests/graph/test_agent_node_resume.py
(test_agent_node_park_then_resume_completes) but with watch_files as the
yielding tool instead of ask_user.
"""
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
    return Agent(id="x", description="watcher",
                 model=AgentModel(provider_id="p", model_name="m"),
                 system_prompt=["Watch files."])


class _YieldingLLM:
    """Simulates the watcher agent calling workspace_ext__watch_files and parking."""
    async def list_models(self): return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="watch_files",
                        event_key="watch_files:gsid:tc1",
                        resume_metadata={"paths": ["outline.md"]}),
                tool_call_id="tc1",
                llm_messages=[Message(role="assistant",
                                      parts=[TextPart(text="(calling watch_files)")]).model_dump(mode="json")])
            yield  # pragma: no cover
        return _g()


class _ContinuationLLM:
    """After resume, emits the agent's final reply."""
    async def list_models(self): return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            yield TextDelta(text="Draft ready.", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


def _graph():
    return Graph(id="g", description="b->A->end", nodes=[
        _BeginNode(id="begin"),
        _AgentNodeRef(id="A", agent_id="x"),
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
        async for _ev in it:
            pass
    except YieldToWorker as exc:
        return exc
    return None


@pytest.mark.asyncio
async def test_watch_files_agent_node_parks(tmp_path):
    """Agent node calling watch_files must park (YieldToWorker), not fail."""
    ex = await _build(tmp_path, _YieldingLLM(), "gsid")
    raised = await _drain_until_yield(ex.invoke([]))

    assert raised is not None, "watch_files yield must propagate as YieldToWorker park"
    assert raised.graph_checkpoint is not None, "checkpoint must be captured for resume"

    pending = ex._pending_agent_yields
    assert len(pending) == 1
    p = pending[0]
    assert p.tool_name == "watch_files"
    assert p.event_key == "watch_files:gsid:tc1"


@pytest.mark.asyncio
async def test_watch_files_agent_node_resumes_to_completion(tmp_path):
    """After file written, watch_files result delivered -> graph ends completed."""
    # 1. Run to the agent-node yield; capture the checkpoint.
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    # 2. Fresh executor (as the worker builds after resume) resumes with the result.
    ex2 = await _build(tmp_path, _ContinuationLLM(), "gsid")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="outline.md changed")])
    async for _ev in ex2.resume_from_checkpoint(
            checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result):
        pass

    state = await ex2.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"
    assert state["node_states"]["A"]["status"] == "ended"
