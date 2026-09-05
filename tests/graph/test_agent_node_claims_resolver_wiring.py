"""Graph-surface boundary (d) - node-qualified resolver + barrier wiring
(Phase 3 stage 7a, 01a0518b).

``_make_node_scoped_call_resolver`` is the graph-surface sibling of
``primer.session.dispatch._make_scoped_call_resolver``: same two-step
lookup, keyed ``(node_id, raw_id)`` instead of ``(None, raw_id)``
(review ruling condition 3 - the ``(None, raw_id)`` key must never
appear on this surface). ``_stream_agent_node`` (live fan-out, shares a
queue with concurrent siblings) additionally binds an
``await_dispatch_barrier`` hook; ``_resume_agent_node`` (direct resume
generator, no concurrent siblings) does not - see both methods' own
comments in primer/graph/_agent_node.py for the full reasoning.
"""

from collections.abc import AsyncIterator

import pytest

from primer.graph._agent_node import _make_node_scoped_call_resolver
from primer.graph.base import _PendingAgentYield
from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, ToolResultPart
from primer.model.graph import (
    Graph, GraphNodeMessage, GraphThread,
    _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel
from primer.session.persistence import _CoalesceState

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


class _SimpleLLM:
    """Says one word and stops - no tool calls, nothing to dispatch. The
    wiring proof only needs run_agent_turn to be CALLED, not for it to
    actually enter tool dispatch."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            yield TextDelta(text="hi", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        return _g()


class _YieldingLLM:
    """Always yields on an ask_user-shaped tool - used to produce a real
    _PendingAgentYield to resume from."""

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
            yield  # pragma: no cover
        return _g()


def _make_coalesce_state(node_id: str, raw_id: str, scoped_id: str, record_seq: int) -> _CoalesceState:
    cs = _CoalesceState()
    cs.scoped_call_ids[(node_id, raw_id)] = scoped_id
    cs.tool_call_record_seq[scoped_id] = record_seq
    return cs


# ===========================================================================
# _make_node_scoped_call_resolver - pure function
# ===========================================================================


def test_resolver_is_node_qualified_not_none_keyed() -> None:
    """The (None, raw_id) key (chat/workspace surface) must never be
    consulted here - only (node_id, raw_id)."""
    cs = _make_coalesce_state("A", "call-1", "A:tool:0:1", 7)
    # A DIFFERENT node happens to reuse the same raw id - must not resolve.
    cs.scoped_call_ids[("B", "call-1")] = "B:tool:0:1"
    cs.tool_call_record_seq["B:tool:0:1"] = 9

    resolve_a = _make_node_scoped_call_resolver(cs, "A")
    assert resolve_a("call-1") == ("A:tool:0:1", 7)

    resolve_b = _make_node_scoped_call_resolver(cs, "B")
    assert resolve_b("call-1") == ("B:tool:0:1", 9)


def test_resolver_raises_loudly_on_missing_scoped_id() -> None:
    cs = _CoalesceState()
    resolve = _make_node_scoped_call_resolver(cs, "A")
    with pytest.raises(RuntimeError, match="no scoped id"):
        resolve("call-missing")


def test_resolver_raises_loudly_on_missing_record_seq() -> None:
    cs = _CoalesceState()
    cs.scoped_call_ids[("A", "call-1")] = "A:tool:0:1"
    # record_seq never populated - the durable-append-before-claimable
    # invariant broke.
    resolve = _make_node_scoped_call_resolver(cs, "A")
    with pytest.raises(RuntimeError, match="no durable TOOL_CALL record_seq"):
        resolve("call-1")


# ===========================================================================
# _stream_agent_node - live fan-out path (resolver + barrier)
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_agent_node_threads_node_qualified_resolver_and_barrier(
    monkeypatch,
) -> None:
    ex = await _mk_executor(_graph(), _SimpleLLM())
    cs = _make_coalesce_state("A", "call-1", "A:tool:0:1", 7)
    ex.bind_coalesce_state(cs)

    captured: dict = {}
    import primer.graph._agent_node as agent_node_mod
    real_run_agent_turn = agent_node_mod.run_agent_turn

    async def _spy(**kwargs):
        captured["resolve_scoped_call"] = kwargs.get("resolve_scoped_call")
        captured["await_dispatch_barrier"] = kwargs.get("await_dispatch_barrier")
        async for ev in real_run_agent_turn(**kwargs):
            yield ev

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    evs = [ev async for ev in ex.invoke([])]
    assert len(evs) > 0

    resolver = captured["resolve_scoped_call"]
    assert resolver is not None
    assert resolver("call-1") == ("A:tool:0:1", 7)

    barrier = captured["await_dispatch_barrier"]
    assert barrier is not None
    assert callable(barrier)


@pytest.mark.asyncio
async def test_stream_agent_node_resolver_is_none_without_bind(monkeypatch) -> None:
    """bind_coalesce_state was never called (today's default, every
    existing graph session) - both hooks must stay None, byte-identical
    to pre-01a0518b behaviour."""
    ex = await _mk_executor(_graph(), _SimpleLLM())

    captured: dict = {}
    import primer.graph._agent_node as agent_node_mod
    real_run_agent_turn = agent_node_mod.run_agent_turn

    async def _spy(**kwargs):
        captured["resolve_scoped_call"] = kwargs.get("resolve_scoped_call")
        captured["await_dispatch_barrier"] = kwargs.get("await_dispatch_barrier")
        async for ev in real_run_agent_turn(**kwargs):
            yield ev

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    [ev async for ev in ex.invoke([])]

    assert captured["resolve_scoped_call"] is None
    assert captured["await_dispatch_barrier"] is None


# ===========================================================================
# _resume_agent_node - direct resume generator (resolver only, no barrier)
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_agent_node_threads_node_qualified_resolver_no_barrier(
    monkeypatch,
) -> None:
    ex = await _mk_executor(_graph(), _YieldingLLM())
    evs = []
    pending = None
    try:
        async for ev in ex.invoke([]):
            evs.append(ev)
    except YieldToWorker:
        pass
    assert len(ex._pending_agent_yields) == 1
    pending = ex._pending_agent_yields[0]

    cs = _make_coalesce_state("A", "call-x", "A:tool:0:9", 3)
    ex.bind_coalesce_state(cs)

    captured: dict = {}
    import primer.graph._agent_node as agent_node_mod
    real_run_agent_turn = agent_node_mod.run_agent_turn

    async def _spy(**kwargs):
        captured["resolve_scoped_call"] = kwargs.get("resolve_scoped_call")
        assert "await_dispatch_barrier" not in kwargs or kwargs["await_dispatch_barrier"] is None
        async for ev in real_run_agent_turn(**kwargs):
            yield ev

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _spy)

    tool_result_msg = Message(role="tool", parts=[ToolResultPart(id="tc1", output="blue")])
    try:
        await ex._resume_agent_node(pending, tool_result_msg)
    except YieldToWorker:
        # _YieldingLLM re-yields on the resumed call too - irrelevant to
        # this test, which only cares about the kwargs threaded through
        # BEFORE that exception fires.
        pass

    resolver = captured["resolve_scoped_call"]
    assert resolver is not None
    assert resolver("call-x") == ("A:tool:0:9", 3)
