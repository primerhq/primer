"""Graph-surface ToolWaitPark ``llm_messages`` stamp (Phase 3 stage 7a,
01a0518b boundary d) - bug fixes in their own right, found while flipping
the graph-side SAFETY GAP flag.

``primer.agent.loop._dispatch_as_claims`` leaves ``ToolWaitPark.
llm_messages`` unset at raise time by contract (mirrors
``YieldToWorker``'s own minimal raise-time contract). One layer up is
responsible for stamping the in-progress turn's own produced messages
onto the exception before it propagates further, so the resume path can
rebuild ``[assistant_tool_use, tool_result]`` history correctly -
``_BaseAgentExecutor`` already does this for the chat/workspace surface
(``primer/agent/base.py``) and ``_stream_agent_node`` already did it for
``YieldToWorker`` on the graph surface. Neither ``_stream_agent_node``
nor ``_resume_agent_node`` had the matching ``except ToolWaitPark`` stamp
arm before this fix - a graph-live (or graph-resume) tool_wait park
would have silently reached the resume path with an EMPTY
``llm_messages``, losing the assistant tool_use message the resume
needs to pair tool results against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.graph.base import _PendingAgentYield
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, TextPart, ToolResultPart
from primer.model.graph import (
    Graph, GraphNodeMessage, GraphThread,
    _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.model_profile import ResolvedModel

from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def _agent() -> Agent:
    return Agent(id="x", description="x", model=AgentModel(profile_id="p--m"))


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="test-profile", provider_id="test-provider", model_name="m",
        context_length=128_000, config=ModelProfileConfig(),
    )


def _graph() -> Graph:
    return Graph(
        id="g", description="b->A->e",
        nodes=[_BeginNode(id="begin"), _AgentNodeRef(id="A", agent_id="x"), _EndNode(id="exit")],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="exit"),
        ],
    )


async def _mk_executor(graph: Graph, llm) -> GraphExecutor:
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
        graph_thread_id=thread.id,
    )


class _UnusedLLM:
    """run_agent_turn itself is monkeypatched in every test here - the
    real LLM is never reached."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        raise AssertionError("real LLM.stream() should never be reached")


class _YieldingLLM:
    """Produces a real _PendingAgentYield to resume from (test 2 needs an
    already-parked node to call _resume_agent_node against)."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:t1:tc1",
                        resume_metadata={"prompt": "color?"}),
                tool_call_id="tc1",
                llm_messages=[{
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "let me check"}],
                }])
            yield  # pragma: no cover - generator marker, unreachable
        return _g()


@pytest.mark.asyncio
async def test_stream_agent_node_stamps_llm_messages_on_tool_wait_park(
    monkeypatch,
) -> None:
    ex = await _mk_executor(_graph(), _UnusedLLM())

    import primer.graph._agent_node as agent_node_mod

    async def _spy(*, messages_out, **kwargs):
        messages_out.append(
            Message(role="assistant", parts=[TextPart(text="calling tool")])
        )
        raise ToolWaitPark(
            outstanding_task_ids=["A:tool:0:1"], event_key="tool_wait:A:tool:0:1",
        )
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    # The exception that ultimately propagates out of invoke() is a FRESH
    # ToolWaitPark built by _build_pending_tool_wait_park (its own
    # llm_messages stays empty by design - a graph resume rebuilds each
    # node's continuation from its OWN _PendingToolWait entry instead, see
    # that builder's docstring) - the stamp under test lands on the node's
    # _PendingToolWait record, not this outer re-raised exception.
    with pytest.raises(ToolWaitPark):
        async for _ev in ex.invoke([]):
            pass

    assert len(ex._pending_tool_waits) == 1
    assert ex._pending_tool_waits[0].llm_messages == [
        {"role": "assistant", "parts": [{"type": "text", "text": "calling tool"}]}
    ]


@pytest.mark.asyncio
async def test_resume_agent_node_stamps_llm_messages_on_tool_wait_park(
    monkeypatch,
) -> None:
    ex = await _mk_executor(_graph(), _YieldingLLM())
    try:
        async for _ev in ex.invoke([]):
            pass
    except YieldToWorker:
        pass
    assert len(ex._pending_agent_yields) == 1
    pending = ex._pending_agent_yields[0]

    import primer.graph._agent_node as agent_node_mod

    async def _spy(*, messages_out, **kwargs):
        messages_out.append(
            Message(role="assistant", parts=[TextPart(text="calling again")])
        )
        raise ToolWaitPark(
            outstanding_task_ids=["A:tool:1:1"], event_key="tool_wait:A:tool:1:1",
        )
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    tool_result_msg = Message(role="tool", parts=[ToolResultPart(id="tc1", output="blue")])
    out_holder: dict = {}
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex._resume_agent_node(pending, tool_result_msg, out_holder):
            pass

    stamped = excinfo.value.llm_messages
    assert stamped is not None
    # The PRIOR park's rehydrated assistant + this resume's own
    # tool_result_msg + this continuation's own new message must all be
    # present - not just the new message alone (which would silently
    # truncate the prefix on a THIRD resume).
    assert stamped[-1] == {
        "role": "assistant", "parts": [{"type": "text", "text": "calling again"}],
    }
    assert any(m.get("role") == "tool" for m in stamped)
    assert any(
        m.get("role") == "assistant"
        and m["parts"][0].get("text") == "let me check"
        for m in stamped
    )
