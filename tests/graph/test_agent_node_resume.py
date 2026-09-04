"""Executor-side resume of a parked agent node: rebuild + continue its turn."""
from collections.abc import AsyncIterator

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, ExtendedEvent, Message, StreamEvent, TextDelta, TextPart,
    ToolResultPart, _GraphNodeEvent,
)
from primer.model.graph import (
    Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.yield_ import Yielded, YieldToWorker

from tests.graph.test_workspace_executor import _make_state_repo
from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig


def _agent():
    return Agent(id="x", description="x",
                 model=AgentModel(profile_id="p--m"),
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


class _ReAskingContinuationLLM:
    """01a06ca4: the resumed turn's OWN continuation yields AGAIN (a
    second ask_user) instead of finishing - e.g. the agent needs one more
    piece of information before it can answer."""
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key="ask_user:t:tc2",
                        resume_metadata={"prompt": "and what shade?"}),
                tool_call_id="tc2",
                llm_messages=[Message(role="assistant",
                                      parts=[TextPart(text="(calling ask_user again)")]).model_dump(mode="json")])
            yield  # pragma: no cover
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
    async def llm_resolver(_): return (llm, ResolvedModel(profile_id="test-profile", provider_id="test-provider", model_name="m", context_length=128_000, config=ModelProfileConfig()))

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


@pytest.mark.asyncio
async def test_resumed_node_continuation_events_reach_the_caller(tmp_path):
    """01a06933: _resume_agent_node's own `async for _event in
    run_agent_turn(...): pass` used to discard everything the resumed
    node's CONTINUING turn produced -- an awaited call has no way to
    yield through resume_from_checkpoint's own generator, so 01a0690a's
    resume-drain tap (which taps resume_from_checkpoint's yielded events
    for the durable record) had nothing to see for this specific case,
    even after that fix landed. Assert the events the continuation LLM
    actually streams (TextDelta + Done, via _ContinuationLLM) now come
    out of resume_from_checkpoint itself, wrapped for node "A" the same
    way the live _stream_agent_node path tags its own forwarded events."""
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid-r3")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    ex2 = await _build(tmp_path, _ContinuationLLM(), "gsid-r3")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="blue")])
    events: list[StreamEvent] = []
    async for ev in ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result):
        events.append(ev)

    node_a_events = [
        ev for ev in events
        if isinstance(ev, ExtendedEvent) and isinstance(ev.extended, _GraphNodeEvent)
        and ev.extended.node_id == "A"
    ]
    assert node_a_events, (
        "the resumed node's own continuation produced no events at all - "
        "pre-fix, _resume_agent_node discarded every one of them"
    )
    inner_types = {ev.extended.inner_type for ev in node_a_events}
    assert "text_delta" in inner_types
    assert "done" in inner_types


@pytest.mark.asyncio
async def test_resume_agent_node_reyield_reparks_not_fails(tmp_path):
    """01a06ca4: _resume_agent_node had no `except YieldToWorker` branch -
    a nested re-yield during resume (the resumed turn asks a SECOND
    ask_user before finishing) fell into the generic `except Exception`
    handler in graph/base.py's ay_pending loop and was mapped to a node
    FAILURE, discarding the in-progress turn entirely. Assert it instead
    re-parks: resume_from_checkpoint raises YieldToWorker again, on the
    NEW event key, carrying a fresh checkpoint built from live state."""
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid-r5")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    ex2 = await _build(tmp_path, _ReAskingContinuationLLM(), "gsid-r5")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="blue")])
    repark = await _drain_until_yield(ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result))

    assert repark is not None, (
        "a mid-resume re-yield must re-park via YieldToWorker, not be "
        "swallowed and mapped to a node failure"
    )
    assert repark.yielded.event_keys == ["ask_user:t:tc2"]
    assert repark.graph_checkpoint is not None

    state = await ex2.load_state()
    assert state is not None
    assert state["status"] == "waiting"
    assert state["node_states"]["A"]["status"] != "failed"

    # A third resume must see the FULL accumulated history (the original
    # user turn + the first ask_user's rehydrated answer + this second
    # ask_user's own in-progress assistant message), not just the second
    # turn in isolation - proves _agent_node.py's except-branch stamped
    # yld.llm_messages with the whole prefix, not produced_messages alone.
    ex3 = await _build(tmp_path, _ContinuationLLM(), "gsid-r5")
    tool_result_2 = Message(role="tool",
                            parts=[ToolResultPart(id="tc2", output="navy")])
    async for _ev in ex3.resume_from_checkpoint(
        repark.graph_checkpoint, resumed_tcid="tc2",
        agent_tool_result=tool_result_2):
        pass

    state3 = await ex3.load_state()
    assert state3 is not None
    assert state3["status"] == "ended"
    assert state3["ended_reason"] == "completed"
    assert state3["node_states"]["A"]["status"] == "ended"


class _CountingYieldLLM:
    def __init__(self): self.n = 0
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        i = self.n; self.n += 1
        async def _g():
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key=f"ask_user:t:tc{i}",
                        resume_metadata={"prompt": f"q{i}"}),
                tool_call_id=f"tc{i}",
                llm_messages=[Message(role="assistant",
                                      parts=[TextPart(text=f"(ask {i})")]).model_dump(mode="json")])
            yield  # pragma: no cover
        return _g()


def _two_ask_graph():
    return Graph(id="g2", description="b->A,B->ends", nodes=[
        _BeginNode(id="begin"),
        _AgentNodeRef(id="A", agent_id="x", input_template="ask A"),
        _AgentNodeRef(id="B", agent_id="x", input_template="ask B"),
        _EndNode(id="ea", output_template="{{ nodes.A.text }}"),
        _EndNode(id="eb", output_template="{{ nodes.B.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="A"),
               _StaticEdge(from_node="begin", to_node="B"),
               _StaticEdge(from_node="A", to_node="ea"),
               _StaticEdge(from_node="B", to_node="eb")])


async def _build2(tmp_path, llm, gsid):
    repo = await _make_state_repo(tmp_path)
    async def agent_resolver(_): return _agent()
    async def llm_resolver(_): return (llm, ResolvedModel(profile_id="test-profile", provider_id="test-provider", model_name="m", context_length=128_000, config=ModelProfileConfig()))
    return WorkspaceGraphExecutor(
        graph=_two_ask_graph(), agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo, graph_session_id=gsid)


@pytest.mark.asyncio
async def test_two_agent_yields_resume_one_reparks_on_other(tmp_path):
    ex1 = await _build2(tmp_path, _CountingYieldLLM(), "gsid-2")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None
    keys = set(raised.yielded.event_keys)
    assert keys == {"ask_user:t:tc0", "ask_user:t:tc1"}
    checkpoint = raised.graph_checkpoint

    # Resume ONE node (tc0); the other (tc1) must keep the session parked.
    ex2 = await _build2(tmp_path, _ContinuationLLM(), "gsid-2")
    tool_result = Message(role="tool", parts=[ToolResultPart(id="tc0", output="a")])
    repark = await _drain_until_yield(ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc0", agent_tool_result=tool_result))
    assert repark is not None, "must re-park while tc1 is still pending"
    assert repark.yielded.event_keys == ["ask_user:t:tc1"]
