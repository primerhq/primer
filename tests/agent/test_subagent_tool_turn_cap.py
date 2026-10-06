"""A subagent that stops at its agent's ``max_tool_turns`` answers its caller with an ERROR that carries what it had
(01a1095e, PR-C).

``run_subagent`` and ``resume_subagent`` called ``run_agent_turn`` without ``capped_out``, so a subagent whose turn
tripped the cap returned the text of its last round as the call's result: ``invoke_agent`` answered
``{"output": "working on step 2"}`` as a SUCCESS and the parent model took a half-done answer for a finished one (the same
class as #414 and #423: a silent success). Now the call fails: ``invoke_agent`` returns an error result that names the
cap and carries the partial text, on the live path (the tool handler) and on the resume path (``AgentFrame.resume``,
which completes the frame as an error ``ToolResultPart``).

Driven through the real ``run_subagent`` / ``resume_subagent`` and the real loop, with the fakes of
``test_run_subagent_yield`` (storage rows, provider registry, a toolset whose one tool always works) and an LLM that asks
for that tool on every call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.invoke import resume_subagent
from primer.api.registries import ProviderRegistry  # noqa: F401  (the system toolset's registry type)
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamStart,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    ToolTurnCapReached,
)
from primer.toolset.system import build_system_toolset
from primer.worker.frames import AgentFrame, AgentResumeContext, Completed
from tests.agent.test_run_subagent_yield import (
    _GatedToolsetProvider,
    _PoliciesOnlyResolver,
    _provider_row,
    _ProviderRegistry,
    _StorageProvider,
)

CAP = 2


class _AlwaysCallsATool:
    """Every call answers with a line of text and a call of the toolset's one tool; it never stops on its own."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, *, model, messages, **kwargs) -> AsyncIterator:  # noqa: ANN001
        self.calls += 1
        n = self.calls

        async def _g() -> AsyncIterator:
            yield StreamStart(model="m1")
            yield TextDelta(text=f"working on step {n}", index=0)
            yield ToolCallStart(id=f"call-{n}", name="t1__do_it", index=1)
            yield ToolCallEnd(id=f"call-{n}", arguments={}, index=1)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return _g()


def _agent() -> Agent:
    return Agent(
        id="agent-sub", description="subagent", model=AgentModel(profile_id="prov-1--m1"),
        system_prompt=["you are a subagent"], tools=["t1__do_it"], max_tool_turns=CAP,
    )


def _fakes() -> tuple[_StorageProvider, _ProviderRegistry, _AlwaysCallsATool]:
    llm = _AlwaysCallsATool()
    storage = _StorageProvider(agent=_agent(), provider_row=_provider_row())
    return storage, _ProviderRegistry(llm=llm, toolset=_GatedToolsetProvider()), llm  # type: ignore[arg-type]


def _what_the_caller_is_told(is_error: bool, body: str) -> list[str]:
    """What a correct fix must show, whichever way it words it: the call is an error, it names the cap, and the text the
    subagent had so far reaches the caller."""
    problems = []
    if not is_error:
        problems.append(f"the capped subagent's call is a SUCCESS: {body}")
    if "tool-turn cap" not in body and "tool_turn_cap" not in body:
        problems.append(f"the result does not say the subagent stopped at its tool-turn cap: {body}")
    if "working on step" not in body:
        problems.append(f"the text the subagent had so far is not carried: {body}")
    return problems


# --- the live path: the invoke_agent tool -----------------------------------------------------------------------


async def _invoke_agent_tool():
    storage, registry, llm = _fakes()
    toolset = build_system_toolset(
        storage_provider=storage, provider_registry=registry,  # type: ignore[arg-type]
        approval_resolver=_PoliciesOnlyResolver([]),
    )
    res = await toolset.call(
        tool_name="invoke_agent", arguments={"agent_id": "agent-sub", "prompt": "go"}, principal=None, ctx=None,
    )
    return res, llm


async def test_scenario_the_subagent_really_stopped_at_its_cap() -> None:
    res, llm = await _invoke_agent_tool()

    assert llm.calls == CAP, "the subagent's turn did not run to its cap: the harness did not create the situation"
    assert res.output, "the call returned nothing at all"


async def test_invoke_agent_answers_a_capped_subagent_with_an_error_that_carries_its_text() -> None:
    res, _ = await _invoke_agent_tool()

    problems = _what_the_caller_is_told(res.is_error, res.output)
    assert not problems, "\n".join(problems)


# --- the resume path: AgentFrame.resume -------------------------------------------------------------------------


class _Services:
    """The continuation walk's services: the REAL ``resume_subagent`` over the fakes."""

    def __init__(self, storage: Any, registry: Any) -> None:
        self._storage, self._registry = storage, registry

    async def resume_subagent(self, **kw: Any) -> str:
        return await resume_subagent(storage_provider=self._storage, provider_registry=self._registry, **kw)


async def _resumed_frame() -> tuple[Completed, _AlwaysCallsATool]:
    storage, registry, llm = _fakes()
    frame = AgentFrame(
        agent_id="agent-sub", tool_call_id="inv-tc", depth=1,
        llm_messages=[{"role": "user", "parts": [{"type": "text", "text": "go"}]}],
        context=AgentResumeContext("ses", "ws", None, "u", ["t1__do_it"]),
    )
    out = await frame.resume(ToolResultPart(id="x", output="child", error=False), _Services(storage, registry))
    assert isinstance(out, Completed)
    return out, llm


async def test_scenario_the_resumed_subagent_really_stopped_at_its_cap() -> None:
    out, llm = await _resumed_frame()

    assert llm.calls == CAP, "the resumed subagent did not run to its cap: the harness did not create the situation"
    assert out.value.id == "inv-tc"


async def test_a_resumed_subagent_that_stops_at_its_cap_completes_its_frame_as_an_error_with_its_text() -> None:
    out, _ = await _resumed_frame()

    problems = _what_the_caller_is_told(out.value.error, json.dumps(json.loads(out.value.output)))
    assert not problems, "\n".join(problems)


# --- the exception ------------------------------------------------------------------------------------------------


def test_the_partial_text_is_the_last_assistant_text_that_said_anything_and_the_body_is_one_shape() -> None:
    produced = [
        Message(role="assistant", parts=[TextPart(text="first thought"), ToolCallPart(id="c1", name="t__a", arguments={})]),
        Message(role="tool", parts=[ToolResultPart(id="c1", output="ok", error=False)]),
        Message(role="assistant", parts=[ToolCallPart(id="c2", name="t__a", arguments={})]),  # a round with no text
        Message(role="tool", parts=[ToolResultPart(id="c2", output="not executed: tool-turn cap reached", error=True)]),
    ]

    exc = ToolTurnCapReached.after(agent_id="a", max_tool_turns=2, produced=produced, subject="subagent 'a'")

    assert exc.partial_text == "first thought"
    assert exc.ended_detail_code == "tool_turn_cap"
    assert str(exc) == "subagent 'a' stopped at its tool-turn cap (max_tool_turns=2) before it finished"
    body = exc.result_body()
    assert (body["type"], body["agent_id"], body["max_tool_turns"], body["partial_output"]) == (
        "tool-turn-cap", "a", 2, "first thought",
    )
    assert "tool-turn cap" in body["message"]


def test_a_turn_that_said_nothing_has_an_empty_partial_text() -> None:
    exc = ToolTurnCapReached.after(agent_id="a", max_tool_turns=1, produced=[])

    assert exc.partial_text == "" and str(exc).startswith("agent 'a' stopped at its tool-turn cap")
