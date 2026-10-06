"""A graph agent node that stops at its agent's ``max_tool_turns`` FAILS, with the code ``tool_turn_cap`` (01a1095e, PR-C).

``_stream_agent_node`` and ``_resume_agent_node`` called ``run_agent_turn`` without ``capped_out``, so the loop's cap
trip (every call of the capped round answered ``not executed: tool-turn cap reached`` and the turn returned) looked like
an ordinary finish: the node ended COMPLETED with whatever text the last round had, the graph carried on with a
half-done result, and its End node rendered it. The same class as a tool that failed (#423) or a child graph that
failed (#414): a node that did not finish is not a success.

The node now fails with ``ended_detail="tool_turn_cap"`` (a ``_GraphErrorEvent`` with that code, the run ends
``failed``), on the live path and on the resume path. Its turn's history is still persisted (every call answered), so the
node can run again under a graph that tolerates the failure.

Driven through the real ``WorkspaceGraphExecutor`` with a scripted LLM that asks for a tool on every call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Message,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests.graph.test_agent_node_resume import _drain_until_yield, _YieldingLLM
from tests.graph.test_workspace_executor import _make_state_repo

CAP = 2


class _AlwaysCallsATool:
    """Every call answers with a line of text and a tool call, and never stops on its own."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self):
        return ["m"]

    def stream(self, **_kw: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        n = self.calls

        async def _g() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text=f"working on step {n}", index=0)
            yield ToolCallStart(id=f"call-{n}", name="nowhere__nothing", index=1)
            yield ToolCallEnd(id=f"call-{n}", arguments={}, index=1)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return _g()


class _OneToolThenStops(_AlwaysCallsATool):
    def stream(self, **kw: Any) -> AsyncIterator[StreamEvent]:
        if self.calls == 0:
            return super().stream(**kw)
        self.calls += 1

        async def _g() -> AsyncIterator[StreamEvent]:
            yield TextDelta(text="all done", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _g()


def _graph() -> Graph:
    return Graph(
        id="g-cap", description="begin -> A -> exit",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x", input_template="go"),
            _EndNode(id="exit", output_template="{{ nodes.A.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="A", to_node="exit")],
    )


async def _build(tmp_path: Path, llm: Any, gsid: str) -> WorkspaceGraphExecutor:
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(_: str) -> Agent:
        return Agent(
            id="x", description="x", model=AgentModel(profile_id="p--m"), system_prompt=["Be terse."],
            max_tool_turns=CAP,
        )

    async def llm_resolver(_: Agent):
        return llm, ResolvedModel(
            profile_id="p", provider_id="prov", model_name="m", context_length=128_000, config=ModelProfileConfig(),
        )

    return WorkspaceGraphExecutor(
        graph=_graph(), agent_resolver=agent_resolver, llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo, graph_session_id=gsid,
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


def _what_the_run_says_about_the_cap(events: list[StreamEvent], state: dict[str, Any]) -> list[str]:
    """What a correct fix must show, whichever way it is written: the node failed with the cap's code, the run failed
    with it, and the graph's End did not render a half-done result."""
    problems = []
    codes = [(e.code, e.node_id) for e in events if isinstance(e, _GraphErrorEvent)]
    if ("tool_turn_cap", "A") not in codes:
        problems.append(f"no _GraphErrorEvent(code='tool_turn_cap', node_id='A'): {codes}")
    if any(isinstance(e, _GraphEndOutputEvent) for e in events):
        problems.append("the End node rendered an output for a node that did not finish")
    if state.get("ended_reason") != "failed" or state.get("ended_detail") != "tool_turn_cap":
        problems.append(f"the run ended {state.get('ended_reason')!r}/{state.get('ended_detail')!r}, expected failed/tool_turn_cap")
    if state["node_states"]["A"]["status"] != "failed":
        problems.append(f"node A is {state['node_states']['A']['status']!r}, expected 'failed'")
    return problems


# --- the live path --------------------------------------------------------------------------------------------


async def test_scenario_the_agent_node_really_stopped_at_its_cap(tmp_path: Path) -> None:
    llm = _AlwaysCallsATool()
    executor = await _build(tmp_path, llm, "gs-live-scenario")

    await _drain(executor.invoke([]))

    assert llm.calls == CAP, "the node's turn did not run to its cap: the harness did not create the situation"
    history = await executor._load_node_history("A")  # noqa: SLF001
    assert history[-1].role == "tool" and "tool-turn cap reached" in str(history[-1].parts[0].output), (
        "the capped round's call must be answered in the persisted history, or every later request is invalid"
    )


async def test_a_node_that_stops_at_its_tool_turn_cap_fails(tmp_path: Path) -> None:
    executor = await _build(tmp_path, _AlwaysCallsATool(), "gs-live")

    events = await _drain(executor.invoke([]))

    state = await executor.load_state()
    assert state is not None
    problems = _what_the_run_says_about_the_cap(events, state)
    assert not problems, "\n".join(problems)


async def test_a_node_whose_model_stops_before_the_cap_still_completes(tmp_path: Path) -> None:
    llm = _OneToolThenStops()
    executor = await _build(tmp_path, llm, "gs-live-control")

    events = await _drain(executor.invoke([]))

    state = await executor.load_state()
    assert state is not None
    assert (state["ended_reason"], state["node_states"]["A"]["status"]) == ("completed", "ended")
    assert [e.text for e in events if isinstance(e, _GraphEndOutputEvent)] == ["all done"]


# --- the resume path -------------------------------------------------------------------------------------------


async def _parked_then_resumed(tmp_path: Path, gsid: str) -> tuple[WorkspaceGraphExecutor, _AlwaysCallsATool]:
    """Park node A on an ask_user, then resume it with a model that never stops asking for tools."""
    first = await _build(tmp_path, _YieldingLLM(), gsid)
    parked = await _drain_until_yield(first.invoke([]))
    assert parked is not None and parked.graph_checkpoint is not None
    llm = _AlwaysCallsATool()
    resumed = await _build(tmp_path, llm, gsid)
    answer = Message(role="tool", parts=[ToolResultPart(id="tc1", output="blue")])
    await _drain(resumed.resume_from_checkpoint(parked.graph_checkpoint, resumed_tcid="tc1", agent_tool_result=answer))
    return resumed, llm


async def test_scenario_the_resumed_node_really_stopped_at_its_cap(tmp_path: Path) -> None:
    resumed, llm = await _parked_then_resumed(tmp_path, "gs-resume-scenario")

    assert llm.calls == CAP, "the resumed turn did not run to the cap: the harness did not create the situation"


async def test_a_resumed_node_that_stops_at_its_tool_turn_cap_fails(tmp_path: Path) -> None:
    resumed, _ = await _parked_then_resumed(tmp_path, "gs-resume")

    state = await resumed.load_state()
    assert state is not None
    problems = [p for p in _what_the_run_says_about_the_cap([], state) if not p.startswith("no _GraphErrorEvent")]
    assert not problems, "\n".join(problems)
