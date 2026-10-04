"""Executor-side resume of a parked agent node: rebuild + continue its turn."""
from collections.abc import AsyncIterator

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, ExtendedEvent, Message, StreamEvent, TextDelta, TextPart,
    ToolCallPart, ToolResultPart, _GraphNodeEvent,
)
from primer.model.graph import (
    Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge,
)
from primer.model.yield_ import ToolWaitPark, Yielded, YieldToWorker
from primer.worker.frames import (
    AgentFrame, AgentResumeContext, frames_from_jsonable,
)

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


async def _build(tmp_path, llm, gsid, graph=None):
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(_): return _agent()
    async def llm_resolver(_): return (llm, ResolvedModel(profile_id="test-profile", provider_id="test-provider", model_name="m", context_length=128_000, config=ModelProfileConfig()))

    return WorkspaceGraphExecutor(
        graph=graph or _graph(), agent_resolver=agent_resolver,
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


_NESTED_INVOKE_TCID = "invoke-tc2"
_NESTED_LEAF_TCID = "leaf-tc2"


def _subagent_frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub",
        llm_messages=[{"role": "assistant", "parts": []}],
        tool_call_id=_NESTED_INVOKE_TCID,
        depth=0,
        context=AgentResumeContext(
            session_id="s", workspace_id="w", chat_id=None,
            principal="p", tools=["misc__ask_user"],
        ),
    )


class _NestedReAskingContinuationLLM:
    """01a06ca4 follow-up: the resumed turn's continuation makes a FRESH
    nested invoke_agent call whose subagent itself yields (mirrors
    test_graph_node_subagent_yield.py's _NestedYieldLLM, applied to the
    RESUME path instead of first dispatch) - the re-park must preserve
    yld.frames/leaf, not just the leaf tool_call_id."""
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        async def _g():
            yld = YieldToWorker(
                Yielded(tool_name="ask_user",
                        event_key=f"ask_user:s:{_NESTED_LEAF_TCID}",
                        resume_metadata={"prompt": "nested?"}),
                tool_call_id=_NESTED_LEAF_TCID,
                llm_messages=[Message(role="assistant",
                                      parts=[TextPart(text="(calling invoke_agent)")]).model_dump(mode="json")])
            yld.frames = [_subagent_frame()]
            raise yld
            yield  # pragma: no cover
        return _g()


@pytest.mark.asyncio
async def test_resume_agent_node_reyield_preserves_nested_frames(tmp_path):
    """Lead-flagged gap on f1dc8b4: the re-park branch's _PendingAgentYield
    must mirror _node_dispatch.py's first-park constructor's frames/leaf
    handling byte-for-byte, or a resumed turn's fresh nested invoke_agent
    yield gets its subagent chain silently dropped on re-park - the NEXT
    resume would then skip the continuation walk and splice the answer
    straight into the outer turn as the invoke_agent result."""
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid-r6")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    ex2 = await _build(tmp_path, _NestedReAskingContinuationLLM(), "gsid-r6")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="blue")])
    repark = await _drain_until_yield(ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result))

    assert repark is not None, "nested subagent yield must still re-park"
    assert len(ex2._pending_agent_yields) == 1
    p = ex2._pending_agent_yields[0]
    assert p.tool_call_id == _NESTED_LEAF_TCID
    assert p.event_key == f"ask_user:s:{_NESTED_LEAF_TCID}"
    assert p.leaf is not None and p.leaf["tool_name"] == "ask_user"
    assert len(p.frames) == 1
    restored_frames = frames_from_jsonable(p.frames)
    assert isinstance(restored_frames[0], AgentFrame)
    assert restored_frames[0].agent_id == "sub"
    assert restored_frames[0].tool_call_id == _NESTED_INVOKE_TCID

    # Checkpoint round-trip: the fresh park this re-yield produced must
    # carry the frames/leaf through snapshot_state/restore_state too.
    ck = ex2.snapshot_state()
    import json
    json.dumps(ck)  # must be JSON-able
    ex3 = await _build(tmp_path, _NestedReAskingContinuationLLM(), "gsid-r6")
    ex3.restore_state(ck)
    p2 = ex3._pending_agent_yields[0]
    assert len(p2.frames) == 1
    assert p2.leaf is not None and p2.leaf["tool_name"] == "ask_user"


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


class _FailingContinuationLLM:
    """01a070d6: the resumed turn's OWN LLM call ends in a terminal
    Error (e.g. a connect failure) with no content - run_agent_turn
    raises TurnStreamFailure instead of quietly returning."""
    async def list_models(self): return ["m"]
    def stream(self, **kw) -> AsyncIterator[StreamEvent]:
        from primer.model.chat import Error

        async def _g():
            yield Error(code="llm_connect_error", message="connect failed", fatal=True)
        return _g()


@pytest.mark.asyncio
async def test_agent_node_resume_llm_failure_ends_failed_with_code(tmp_path):
    """01a070d6: before this fix, resume_from_checkpoint's ay_pending
    loop hardcoded ended_detail="tool_execution_failed" for EVERY
    exception reaching its generic except-Exception branch - an actively
    wrong label for an LLM stream failure, since nothing about a tool
    ran. Must now carry the classifier's own code instead."""
    ex1 = await _build(tmp_path, _YieldingLLM(), "gsid-r-fail")
    raised = await _drain_until_yield(ex1.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    ex2 = await _build(tmp_path, _FailingContinuationLLM(), "gsid-r-fail")
    tool_result = Message(role="tool",
                          parts=[ToolResultPart(id="tc1", output="blue")])
    async for _ev in ex2.resume_from_checkpoint(
        checkpoint, resumed_tcid="tc1", agent_tool_result=tool_result):
        pass

    state = await ex2.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "failed"
    assert state["ended_detail"] == "llm_connect_error"
    assert state["node_states"]["A"]["status"] == "failed"


# --- a node that parks more than once must see its user input exactly once ---

_USER_INPUT = "do the thing"


def _input_graph():
    """Like _graph(), but the node has an explicit input_template so its user
    message is recognisable in a prompt."""
    return Graph(id="gi", description="b->A->e", nodes=[
        _BeginNode(id="begin"),
        _AgentNodeRef(id="A", agent_id="x", input_template=_USER_INPUT),
        _EndNode(id="exit", output_template="{{ nodes.A.text }}")],
        edges=[_StaticEdge(from_node="begin", to_node="A"),
               _StaticEdge(from_node="A", to_node="exit")])


def _shape(messages) -> list[tuple[str, str]]:
    """A prompt as (role, what-it-carries) pairs, easy to compare exactly."""
    out: list[tuple[str, str]] = []
    for m in messages:
        for part in m.parts:
            if isinstance(part, TextPart):
                out.append((m.role, part.text))
            elif isinstance(part, ToolCallPart):
                out.append((m.role, f"call:{part.id}"))
            elif isinstance(part, ToolResultPart):
                out.append((m.role, f"result:{part.id}:{part.output}"))
    return out


class _ParkingTurn:
    """Stands in for ``run_agent_turn``: records the prompt it is handed. For its
    first ``parks`` calls it appends an assistant tool call to ``messages_out`` and
    raises a bare ``YieldToWorker`` with ``llm_messages`` UNSET, exactly as a real
    ask_user / approval yield leaves it, so it is the node's own except-arm that
    stamps the in-progress turn. After that it answers."""

    def __init__(self, parks: int) -> None:
        self.parks = parks
        self.prompts: list[list[Message]] = []

    async def __call__(self, *, prompt, messages_out, **_kwargs):
        i = len(self.prompts)
        self.prompts.append(list(prompt))
        if i < self.parks:
            messages_out.append(Message(role="assistant", parts=[
                ToolCallPart(id=f"tc{i}", name="ask_user", arguments={"q": i})]))
            raise YieldToWorker(
                Yielded(tool_name="ask_user", event_key=f"ask_user:t:tc{i}",
                        resume_metadata={"prompt": f"q{i}"}),
                tool_call_id=f"tc{i}")
        messages_out.append(Message(role="assistant", parts=[TextPart(text="done")]))
        yield TextDelta(text="done", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


def _answer(i: int) -> Message:
    return Message(role="tool", parts=[ToolResultPart(id=f"tc{i}", output=f"answer{i}")])


@pytest.mark.asyncio
async def test_a_node_that_parks_three_times_sees_its_user_input_once_each_time(tmp_path, monkeypatch):
    """The re-park stamp used to begin with the node's own user message, and every
    resume prepends a freshly rendered one: after a second park the NEXT resume's
    prompt (and what was persisted to the node history) carried the instruction
    twice, and a third park carried it again. Two human approvals, or an ask_user
    followed by an approval, were enough to reach it.

    Park, resume, park, resume, park, resume: every prompt the model is handed, and
    the node history that ends up on disk, must hold the user input exactly once and
    every earlier turn exactly once."""
    import primer.graph._agent_node as agent_node_mod

    turn = _ParkingTurn(parks=3)
    monkeypatch.setattr(agent_node_mod, "run_agent_turn", turn)

    ex = await _build(tmp_path, object(), "gsid-3p", _input_graph())
    raised = await _drain_until_yield(ex.invoke([]))
    assert raised is not None and raised.graph_checkpoint is not None
    checkpoint = raised.graph_checkpoint

    for i in range(3):
        ex = await _build(tmp_path, object(), "gsid-3p", _input_graph())
        again = await _drain_until_yield(ex.resume_from_checkpoint(
            checkpoint, resumed_tcid=f"tc{i}", agent_tool_result=_answer(i)))
        if i < 2:
            assert again is not None and again.graph_checkpoint is not None, f"resume {i} must re-park"
            # The stamp is the in-progress turn WITHOUT the node's user input (the
            # next resume re-renders it). Pinned here as well as through the
            # prompt, because the resume tolerates a stale leading user message
            # and so would hide a stamp that went back to carrying one.
            stamped = ex._pending_agent_yields[0].llm_messages
            assert "user" not in [m["role"] for m in stamped], "a parked stamp never carries the user input"
            checkpoint = again.graph_checkpoint
        else:
            assert again is None, "the last resume answers and the node ends"

    system = ("system", "Be terse.")
    user = ("user", _USER_INPUT)
    assert [_shape(p) for p in turn.prompts] == [
        [system, user],
        [system, user, ("assistant", "call:tc0"), ("tool", "result:tc0:answer0")],
        [system, user, ("assistant", "call:tc0"), ("tool", "result:tc0:answer0"),
         ("assistant", "call:tc1"), ("tool", "result:tc1:answer1")],
        [system, user, ("assistant", "call:tc0"), ("tool", "result:tc0:answer0"),
         ("assistant", "call:tc1"), ("tool", "result:tc1:answer1"),
         ("assistant", "call:tc2"), ("tool", "result:tc2:answer2")],
    ]

    history = await (await _build(tmp_path, object(), "gsid-3p", _input_graph()))._load_node_history("A")
    assert _shape(history) == [
        user,
        ("assistant", "call:tc0"), ("tool", "result:tc0:answer0"),
        ("assistant", "call:tc1"), ("tool", "result:tc1:answer1"),
        ("assistant", "call:tc2"), ("tool", "result:tc2:answer2"),
        ("assistant", "done"),
    ], "the persisted node history carries the user input once"


@pytest.mark.asyncio
async def test_a_checkpoint_stamped_by_the_old_code_resumes_with_the_user_input_once(tmp_path, monkeypatch):
    """The old re-park arms stamped ``[new_user_msg, ...]``, so a node already parked
    twice sits in storage with a stamp that BEGINS with its user input (a node
    re-parked N times, with N copies). Fixing the stamp does not touch those
    records; the resume must not turn them into a duplicated instruction either."""
    import primer.graph._agent_node as agent_node_mod

    turn = _ParkingTurn(parks=1)
    monkeypatch.setattr(agent_node_mod, "run_agent_turn", turn)
    ex = await _build(tmp_path, object(), "gsid-old", _input_graph())
    assert await _drain_until_yield(ex.invoke([])) is not None
    pending = ex._pending_agent_yields[0]

    def dump(message: Message) -> dict:
        return message.model_dump(mode="json")

    user_input = Message(role="user", parts=[TextPart(text=_USER_INPUT)])
    call = lambda i: Message(role="assistant", parts=[ToolCallPart(id=f"tc{i}", name="ask_user", arguments={"q": i})])  # noqa: E731
    # what the old arms stamped for a node parked three times: two leading copies
    pending.llm_messages = [dump(user_input), dump(user_input), dump(call(0)), dump(_answer(0)), dump(call(1))]

    async for _ev in ex._resume_agent_node(pending, _answer(1), {}):
        pass

    assert _shape(turn.prompts[-1]) == [
        ("system", "Be terse."), ("user", _USER_INPUT),
        ("assistant", "call:tc0"), ("tool", "result:tc0:answer0"),
        ("assistant", "call:tc1"), ("tool", "result:tc1:answer1"),
    ]


@pytest.mark.asyncio
async def test_a_tool_wait_re_park_stamp_does_not_carry_the_user_input_either(tmp_path, monkeypatch):
    """The ToolWaitPark arm had the same stamp as the YieldToWorker arm, so a node that
    re-parks on a claims batch duplicated its user input on the next resume too."""
    import primer.graph._agent_node as agent_node_mod

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _ParkingTurn(parks=1))
    ex = await _build(tmp_path, object(), "gsid-tw", _input_graph())
    assert await _drain_until_yield(ex.invoke([])) is not None
    pending = ex._pending_agent_yields[0]

    async def _re_park(*, messages_out, **_kwargs):
        messages_out.append(Message(role="assistant", parts=[TextPart(text="calling again")]))
        raise ToolWaitPark(outstanding_task_ids=["A:tool:1:1"], event_key="tool_wait:A:tool:1:1")
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(agent_node_mod, "run_agent_turn", _re_park)
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex._resume_agent_node(pending, _answer(0), {}):
            pass

    stamped = [Message.model_validate(m) for m in excinfo.value.llm_messages]
    assert _shape(stamped) == [
        ("assistant", "call:tc0"), ("tool", "result:tc0:answer0"), ("assistant", "calling again"),
    ]
