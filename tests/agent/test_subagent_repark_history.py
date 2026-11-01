"""AG-H4: a sub-agent that parks more than once keeps every message after its system and task prefix.

``run_subagent``'s frame holds the whole in-progress conversation; a frame pushed by ``resume_subagent`` on a re-park used
to hold only the continuation's new messages, so the second resume's prompt lost the first call, its result and every
round before the first park.

Characterization (unchanged by the fix): a single park and resume sends the requests it sends today; a legacy frame
(no saved prompt) resumes in its history-only shape; a capped resumed sub-agent's error carries the continuation's text.
"""

from __future__ import annotations

import pytest

from primer.model.chat import Message, StreamStart, TextDelta, Done, ToolCallPart, ToolResultPart, ToolTurnCapReached
from primer.model.principal import PrincipalRef
from primer.model.yield_ import YieldToWorker
from tests.agent.test_subagent_resume import (
    _agent,
    _context,
    _final_text_script,
    _parked_llm_messages,
    _provider_row,
    _ProviderRegistry,
    _RecordingScriptedLLM,
    _StorageProvider,
    _tool_call_script,
    _YieldingToolsetProvider,
)

_ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")


def _shape(request: list[Message]) -> list[tuple[str, list[str]]]:
    """Each message's role and its call/result ids (text parts as ``text``)."""
    return [
        (m.role, [p.id if isinstance(p, (ToolCallPart, ToolResultPart)) else p.type for p in m.parts])
        for m in request
    ]


def _deps(llm, *, tools=("t1__wait",)):
    agent = _agent(tools=list(tools))
    return agent, _StorageProvider(agent=agent, provider_row=_provider_row()), _ProviderRegistry(llm=llm, toolset=_YieldingToolsetProvider())


async def _first_park(storage, registry):
    from primer.agent.invoke import invocation_depth_guard, run_subagent

    with pytest.raises(YieldToWorker) as parked:
        with invocation_depth_guard():
            await run_subagent(
                agent_id="agent-sub", prompt="task", storage_provider=storage, provider_registry=registry,
                session_id="s", workspace_id="w", invoke_tool_call_id="parent", identity=_ADMIN,
            )
    (frame,) = parked.value.frames
    return frame


async def _resume(frame, result, storage, registry):
    from primer.agent.invoke import resume_subagent

    return await resume_subagent(
        agent_id=frame.agent_id, context=frame.context, llm_messages=frame.llm_messages, child_result=result,
        depth=frame.depth, storage_provider=storage, provider_registry=registry, invoke_tool_call_id=frame.tool_call_id,
    )


@pytest.mark.asyncio
async def test_a_second_resume_keeps_the_first_call_and_its_result():
    llm = _RecordingScriptedLLM(scripts=[
        _tool_call_script("t1__wait", "call-1"),   # first run: parks on call-1
        _tool_call_script("t1__wait", "call-2"),   # first resume: parks on call-2
        _final_text_script("done"),                # second resume: answers
    ])
    _, storage, registry = _deps(llm)
    f1 = await _first_park(storage, registry)
    with pytest.raises(YieldToWorker) as second:
        await _resume(f1, ToolResultPart(id="call-1", output="r1"), storage, registry)
    (f2,) = second.value.frames
    out = await _resume(f2, ToolResultPart(id="call-2", output="r2"), storage, registry)

    assert out == "done"
    assert _shape(llm.requests[2]) == [
        ("system", ["text"]), ("user", ["text"]),
        ("assistant", ["call-1"]), ("tool", ["call-1"]),
        ("assistant", ["call-2"]), ("tool", ["call-2"]),
    ]
    results = [p.output for m in llm.requests[2] for p in m.parts if isinstance(p, ToolResultPart)]
    assert results == ["r1", "r2"]


@pytest.mark.asyncio
async def test_a_legacy_frame_that_reparks_keeps_its_history_only_shape():
    """No saved prompt: the resume sends the parked history and the result, and a re-park's frame holds all of it."""
    from primer.agent.invoke import resume_subagent

    llm = _RecordingScriptedLLM(scripts=[_tool_call_script("t1__wait", "call-2"), _final_text_script("done")])
    _, storage, registry = _deps(llm)
    with pytest.raises(YieldToWorker) as parked:
        await resume_subagent(
            agent_id="agent-sub", context=_context(tools=["t1__wait"]), llm_messages=_parked_llm_messages("t1__wait", "call-1"),
            child_result=ToolResultPart(id="call-1", output="r1"), depth=1, storage_provider=storage, provider_registry=registry,
            invoke_tool_call_id="parent",
        )
    (frame,) = parked.value.frames
    assert _shape(llm.requests[0]) == [("assistant", ["call-1"]), ("tool", ["call-1"])]
    await _resume(frame, ToolResultPart(id="call-2", output="r2"), storage, registry)
    assert _shape(llm.requests[1]) == [
        ("assistant", ["call-1"]), ("tool", ["call-1"]), ("assistant", ["call-2"]), ("tool", ["call-2"]),
    ]


@pytest.mark.asyncio
async def test_a_single_park_and_resume_sends_the_same_requests():
    llm = _RecordingScriptedLLM(scripts=[_tool_call_script("t1__wait", "call-1"), _final_text_script("done")])
    _, storage, registry = _deps(llm)
    f1 = await _first_park(storage, registry)
    assert await _resume(f1, ToolResultPart(id="call-1", output="r1"), storage, registry) == "done"
    assert _shape(llm.requests[0]) == [("system", ["text"]), ("user", ["text"])]
    assert _shape(llm.requests[1]) == [("system", ["text"]), ("user", ["text"]), ("assistant", ["call-1"]), ("tool", ["call-1"])]


@pytest.mark.asyncio
async def test_a_capped_resumed_subagent_reports_the_continuations_text():
    """``max_tool_turns=2``: the resume's second round trips the cap; the error carries what the continuation said (nothing)."""
    def _talk_then_call(text, call_id):
        return [StreamStart(model="m1"), TextDelta(text=text, index=0), *_tool_call_script("t1__wait", call_id)[1:]]

    llm = _RecordingScriptedLLM(scripts=[
        _talk_then_call("before the park", "call-1"), _tool_call_script("t1__noop", "call-2"), _tool_call_script("t1__noop", "call-3"),
    ])
    agent, storage, registry = _deps(llm)
    agent.max_tool_turns = 2
    f1 = await _first_park(storage, registry)
    with pytest.raises(ToolTurnCapReached) as capped:
        await _resume(f1, ToolResultPart(id="call-1", output="r1"), storage, registry)
    assert "before the park" not in str(capped.value)
    assert capped.value.partial_text == ""
