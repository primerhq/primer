"""Task 6.1 - a yield that ORIGINATES in a subagent invoked by a graph
agent-node must park the graph session preserving the nested subagent frames,
and resume must re-descend (continuation walk) before delivering the unwound
result into the parked node.

Two layers:

* DESCENT (faithful executor): a graph agent-node whose agent turn raises a
  :class:`YieldToWorker` carrying ``.frames == [AgentFrame(subagent)]`` (exactly
  what ``run_subagent`` produces when the subagent's ask_user yields) and
  ``.yielded`` = the subagent's real leaf. The graph must record a
  ``_PendingAgentYield`` that PRESERVES those frames + leaf, and the checkpoint
  must round-trip them. The node's own ask_user park (no frames) is unchanged.

* RESUME (pool branch, fakes): the worker's ``_resume_graph_engine`` nested
  branch runs the continuation walk, then either delivers the result into the
  node (Deliver) or re-parks the graph session (Repark).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import StreamEvent
from primer.model.graph import (
    Graph, GraphNodeMessage, GraphThread,
    _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker
from primer.worker.frames import AgentFrame, AgentResumeContext, frames_from_jsonable

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


# ---------------------------------------------------------------------------
# DESCENT: a graph agent-node yields from inside a nested invoke_agent.
# ---------------------------------------------------------------------------


# The invoke_agent call id (the node's own pending call) and the deeper leaf
# tcid (the subagent's ask_user). run_subagent pushes an AgentFrame keyed by
# the invoke_agent id; the leaf yield is the subagent's ask_user.
_INVOKE_TCID = "invoke-tc"
_LEAF_TCID = "leaf-tc"


def _subagent_frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id=_INVOKE_TCID,
        depth=0,
        context=AgentResumeContext(
            session_id="s", workspace_id="w", chat_id=None,
            principal="p", tools=["misc__ask_user"],
        ),
    )


class _NestedYieldLLM:
    """Simulates a graph node whose agent called invoke_agent and the subagent
    yielded: run_agent_turn raises a YieldToWorker whose ``.frames`` already
    carries the subagent AgentFrame and ``.yielded`` is the deeper leaf."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            yld = YieldToWorker(
                Yielded(
                    tool_name="ask_user",
                    event_key=f"ask_user:s:{_LEAF_TCID}",
                    resume_metadata={"prompt": "color?"},
                ),
                tool_call_id=_LEAF_TCID,
                # The node's OWN mid-flight history (the assistant message that
                # called invoke_agent). Stamped by _stream_agent_node normally;
                # set here so the fake mirrors the real shape.
                llm_messages=[{"role": "assistant", "parts": []}],
            )
            yld.frames = [_subagent_frame()]
            raise yld
            yield  # pragma: no cover
        return _g()


def _agent():
    return Agent(id="x", description="x",
                 model=AgentModel(provider_id="p", model_name="m"))


def _graph():
    return Graph(id="g", description="b->A->e", nodes=[
        _BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x"),
        _EndNode(id="exit")],
        edges=[_StaticEdge(from_node="begin", to_node="A"),
               _StaticEdge(from_node="A", to_node="exit")])


async def _mk_executor(graph, llm):
    async def agent_resolver(_):
        return _agent()

    async def llm_resolver(_):
        return (llm, LLMModel(name="m", context_length=128_000))

    ts: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    ms: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=ts)  # type: ignore[arg-type]
    return GraphExecutor(
        graph=graph, agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        thread_storage=ts, message_storage=ms,  # type: ignore[arg-type]
        graph_thread_id=thread.id)


async def _drain_until_yield(it):
    try:
        async for _ev in it:
            pass
    except YieldToWorker as exc:
        return exc
    return None


@pytest.mark.asyncio
async def test_graph_node_nested_subagent_yield_preserves_frames():
    ex = await _mk_executor(_graph(), _NestedYieldLLM())
    raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None, "nested subagent yield must park the graph"

    # The node recorded a pending entry that PRESERVES the subagent frames +
    # the deeper leaf (not just the node's own call).
    assert len(ex._pending_agent_yields) == 1
    p = ex._pending_agent_yields[0]
    assert p.node_id == "A"
    # The entry awaits the LEAF (so the park key + drain selection track the
    # subagent's real ask_user), while the frame carries the invoke_agent id.
    assert p.tool_call_id == _LEAF_TCID
    assert p.event_key == f"ask_user:s:{_LEAF_TCID}"
    assert p.leaf is not None and p.leaf["tool_name"] == "ask_user"
    assert len(p.frames) == 1
    restored_frames = frames_from_jsonable(p.frames)
    assert isinstance(restored_frames[0], AgentFrame)
    assert restored_frames[0].agent_id == "sub"
    assert restored_frames[0].tool_call_id == _INVOKE_TCID


@pytest.mark.asyncio
async def test_graph_node_nested_frames_roundtrip_checkpoint():
    ex = await _mk_executor(_graph(), _NestedYieldLLM())
    raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None
    import json
    ck = ex.snapshot_state()
    json.dumps(ck)  # must be JSON-able
    ex2 = await _mk_executor(_graph(), _NestedYieldLLM())
    ex2.restore_state(ck)
    assert len(ex2._pending_agent_yields) == 1
    p2 = ex2._pending_agent_yields[0]
    assert len(p2.frames) == 1
    assert p2.leaf is not None and p2.leaf["tool_name"] == "ask_user"
    f = frames_from_jsonable(p2.frames)[0]
    assert isinstance(f, AgentFrame) and f.tool_call_id == _INVOKE_TCID


@pytest.mark.asyncio
async def test_ordinary_agent_node_park_has_no_frames():
    """Regression: an ordinary ask_user node park (no nested invoke_agent)
    records EMPTY frames + None leaf - the byte-identical pre-unification path.
    """
    class _OwnYieldLLM:
        async def list_models(self):
            return ["m"]

        def stream(self, **kw) -> AsyncIterator[StreamEvent]:
            async def _g():
                raise YieldToWorker(
                    Yielded(tool_name="ask_user", event_key="ask_user:s:tc1",
                            resume_metadata={"prompt": "q"}),
                    tool_call_id="tc1",
                    llm_messages=[{"role": "assistant", "parts": []}])
                yield  # pragma: no cover
            return _g()

    ex = await _mk_executor(_graph(), _OwnYieldLLM())
    raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None
    p = ex._pending_agent_yields[0]
    assert p.frames == [] and p.leaf is None
