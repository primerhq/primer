"""The documented graphs are runnable, route on structured output, and the research cookbook's graph actually loops (review of #512, B1).

``test_docs_tool_call_bodies.py`` validates the SHAPE of every documented call against its tool's input schema. That accepted a cookbook graph whose
``json_path`` router read a node with no ``response_format`` (so ``parsed`` is never set and the router always took ``default_to``: the loop back to the
researcher could never happen) and that carried a cycle without ``max_iterations`` (it saves as a draft and is refused when a session binds to it).
Both are meaning, not shape. This file checks the meaning:

* every documented ``system::create_graph`` / ``update_graph`` example is runnable (``Graph.assert_runnable``) and every ``json_path`` router in it
  reads an agent node that has a ``response_format``;
* the research cookbook's graph, read from the page itself, is run through the real ``GraphExecutor`` with scripted agents: a failed source sends the
  run back to the researcher with the failure listed in its input, a clean verdict goes to the writer, a verdict that is not JSON does not loop, and
  a run that never validates lands on the writer at the iteration cap instead of failing.

What this does NOT check: that a real model follows the prompts (the agents are scripted), the tool calls the researcher and fact-checker would make
(the executor here has no tool manager), or graph bodies that appear in the docs outside a documented ``system::`` call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.executor import GraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta
from primer.model.graph import Graph, GraphNodeMessage, GraphThread, _AgentNodeRef, _ConditionalEdge, _JsonPathRouter
from primer.model.workspace_session import SessionStatus
from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig
from tests.docs.test_docs_tool_call_bodies import AGENT_DOCS, REPO, calls_in

COOKBOOK = REPO / "docs" / "agents" / "cookbook" / "multi-agent-graph-research.md"
GRAPH_CALLS = ("create_graph", "update_graph")


def documented_graphs(text: str) -> list[dict[str, Any]]:
    """The ``entity`` of every ``system::create_graph`` / ``update_graph`` call in a doc that carries a graph body."""
    found = []
    for call in calls_in(text):
        entity = call.arguments.get("entity") if isinstance(call.arguments, dict) else None
        if call.toolset == "system" and call.tool in GRAPH_CALLS and isinstance(entity, dict) and "nodes" in entity:
            found.append(entity)
    return found


def graph_problems(entity: dict[str, Any]) -> list[str]:
    """What stops a documented graph from running as the page says: it does not validate, it is not runnable, or a json_path router reads a node
    that never produces ``parsed`` (only an agent node with a ``response_format`` does)."""
    try:
        graph = Graph.model_validate(entity)
    except ValueError as exc:
        return [f"{entity.get('id')!r} does not validate: {str(exc)[:200]}"]
    problems = []
    try:
        graph.assert_runnable()
    except ValueError as exc:
        problems.append(str(exc))
    by_id = {node.id: node for node in graph.nodes}
    for edge in graph.edges:
        if isinstance(edge, _ConditionalEdge) and isinstance(edge.router, _JsonPathRouter):
            source = by_id.get(edge.from_node)
            if not (isinstance(source, _AgentNodeRef) and source.response_format is not None):
                problems.append(
                    f"graph {graph.id!r}: the json_path router on {edge.from_node!r} reads a node with no response_format, "
                    "so its parsed output is never set and the router always takes default_to"
                )
    return problems


# ---- every documented graph -----------------------------------------------------------------------------------------------------


def test_the_scan_finds_a_documented_graph() -> None:
    graphs = [g for doc in AGENT_DOCS for g in documented_graphs(doc.read_text(encoding="utf-8"))]

    assert graphs, "no documented system::create_graph / update_graph example with a graph body was found in docs/agents/"


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_documented_graph_is_runnable_and_routes_on_structured_output(doc: Path) -> None:
    problems = [p for entity in documented_graphs(doc.read_text(encoding="utf-8")) for p in graph_problems(entity)]

    assert not problems, f"{doc.relative_to(REPO)} documents a graph that does not run as written:\n" + "\n".join(f"  {p}" for p in problems)


def test_a_router_on_a_node_without_a_response_format_and_an_unbounded_cycle_are_both_found() -> None:
    """The shape the cookbook had before: it validates, so only the meaning checks can refuse it."""
    entity = {
        "id": "g",
        "description": "d",
        "nodes": [
            {"kind": "begin", "id": "begin"},
            {"kind": "agent", "id": "a", "agent_id": "a"},
            {"kind": "end", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "begin", "to_node": "a"},
            {
                "kind": "conditional", "from_node": "a",
                "router": {"kind": "json_path", "branches": [{"conditions": [{"path": "x", "op": "exists"}], "to_node": "a"}], "default_to": "end"},
            },
        ],
    }

    problems = graph_problems(entity)

    assert any("requires max_iterations" in p for p in problems), problems
    assert any("no response_format" in p for p in problems), problems


# ---- the research cookbook's graph, run ---------------------------------------------------------------------------------------


_RUNAWAY_CALLS = 30
_RUN_TIMEOUT_S = 30


class _Scripted:
    """One agent's model: answers with its next scripted reply (the last one repeats) and records every call."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: list[Message], **kwargs: Any):
        # A page that loses its max_iterations would loop forever on an always-failing verdict: fail loudly instead of hanging the lane.
        if len(self.calls) >= _RUNAWAY_CALLS:
            raise AssertionError(f"more than {_RUNAWAY_CALLS} calls to one agent: the graph is looping without a bound")
        reply = self._replies[min(len(self.calls), len(self._replies) - 1)]
        self.calls.append({"messages": list(messages), **kwargs})
        return self._events(reply)

    async def _events(self, reply: str) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text=reply, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    def last_input(self) -> str:
        user = [m for m in self.calls[-1]["messages"] if m.role == "user"]
        return "".join(p.text for p in user[-1].parts if p.type == "text")

    def inputs(self) -> list[str]:
        return ["".join(p.text for p in [m for m in c["messages"] if m.role == "user"][-1].parts if p.type == "text") for c in self.calls]


def _cookbook_graph() -> Graph:
    graphs = documented_graphs(COOKBOOK.read_text(encoding="utf-8"))
    assert len(graphs) == 1, f"expected exactly one graph body in the cookbook, found {len(graphs)}"
    return Graph.model_validate(graphs[0])


def verdict(good: list[str], bad: list[str]) -> str:
    return json.dumps({"good_sources": good, "bad_sources": bad})


async def _run(
    fake_storage_provider,
    replies: dict[str, list[str]],
    question: str = "How did SLOs evolve?",
    *,
    graph_input: Any = None,
    max_iterations: int | None = None,
    land_on_writer: bool = True,
    unbounded: bool = False,
):
    """Run the cookbook graph with one scripted model per agent; return (thread, per-agent models, the order agents were called in).

    ``max_iterations`` overrides the page's cap and ``land_on_writer=False`` drops its ``on_max_iterations``, to check what the page says about both."""
    graph = _cookbook_graph()
    if max_iterations is not None:
        graph = graph.model_copy(update={"max_iterations": max_iterations})
    if not land_on_writer:
        graph = graph.model_copy(update={"on_max_iterations": None})
    if unbounded:
        graph = graph.model_copy(update={"max_iterations": None, "on_max_iterations": None})
    models = {agent_id: _Scripted(script) for agent_id, script in replies.items()}
    order: list[str] = []

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id=agent_id, description=agent_id, model=AgentModel(profile_id="p--m"), system_prompt=[])

    async def llm_resolver(agent: Agent):
        order.append(agent.id)
        resolved = ResolvedModel(
            profile_id="p--m", provider_id="p", model_name="m", context_length=128_000, config=ModelProfileConfig(),
        )
        return models[agent.id], resolved

    threads = fake_storage_provider.get_storage(GraphThread)
    messages = fake_storage_provider.get_storage(GraphNodeMessage)
    thread = await GraphExecutor.open_thread(graph=graph, thread_storage=threads, title="t")
    executor = GraphExecutor(
        graph=graph, agent_resolver=agent_resolver, llm_resolver=llm_resolver,
        thread_storage=threads, message_storage=messages, graph_thread_id=thread.id,
    )
    async with asyncio.timeout(_RUN_TIMEOUT_S):  # the body is bounded too: a run that never ends fails here, it does not hang
        _ = [event async for event in executor.invoke({"question": question} if graph_input is None else graph_input)]
    return await threads.get(thread.id), models, order


A, B, C = "https://a.example", "https://b.example", "https://c.example"


@pytest.mark.asyncio
async def test_a_failed_source_sends_the_run_back_to_the_researcher_with_the_failure_listed(fake_storage_provider) -> None:
    thread, models, order = await _run(
        fake_storage_provider,
        {
            "researcher": [f"Sources: {A}, {B}", f"Sources: {C}"],
            "fact-checker": [f"{A} holds up, {B} is contradicted", f"{C} holds up"],
            "verdict": [verdict([A], [B]), verdict([A, C], [])],
            "writer": ["# Report"],
        },
    )

    assert thread.status == SessionStatus.ENDED and thread.ended_reason == "completed", (thread.ended_reason, thread.ended_detail)
    assert order == [
        "researcher", "fact-checker", "verdict", "researcher", "fact-checker", "verdict", "writer",
    ]
    first, second = models["researcher"].inputs()
    assert "How did SLOs evolve?" in first and "failed validation" not in first
    assert B in second and "do not propose them again" in second and A not in second, "the second pass must list only the failed source"
    assert f"Sources: {A}, {B}" in models["fact-checker"].inputs()[0], "the fact-checker must be given the researcher's sources"
    assert f"{B} is contradicted" in models["verdict"].inputs()[0], "the verdict step must be given the fact-checker's findings"
    assert verdict([A, C], []) in models["writer"].last_input() and "How did SLOs evolve?" in models["writer"].last_input()


@pytest.mark.asyncio
async def test_the_verdict_step_is_asked_for_structured_output_and_the_others_are_not(fake_storage_provider) -> None:
    _, models, _ = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["ok"], "verdict": [verdict([A], [])], "writer": ["# Report"]},
    )

    assert models["verdict"].calls[0].get("response_format"), "the verdict node must carry the response_format"
    for agent_id in ("researcher", "fact-checker", "writer"):
        assert not models[agent_id].calls[0].get("response_format"), agent_id


@pytest.mark.asyncio
async def test_a_clean_verdict_goes_straight_to_the_writer(fake_storage_provider) -> None:
    thread, _, order = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["ok"], "verdict": [verdict([A], [])], "writer": ["# Report"]},
    )

    assert thread.ended_reason == "completed"
    assert order == ["researcher", "fact-checker", "verdict", "writer"]


@pytest.mark.asyncio
async def test_a_verdict_that_is_not_json_does_not_loop_it_goes_to_the_writer(fake_storage_provider) -> None:
    thread, _, order = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["ok"], "verdict": ["I could not decide."], "writer": ["# Report"]},
    )

    assert thread.ended_reason == "completed"
    assert order == ["researcher", "fact-checker", "verdict", "writer"]


@pytest.mark.asyncio
async def test_a_run_that_never_validates_lands_on_the_writer_at_the_cap_instead_of_failing(fake_storage_provider) -> None:
    thread, models, order = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["bad"], "verdict": [verdict([], [A])], "writer": ["# Report"]},
    )

    assert thread.status == SessionStatus.ENDED and thread.ended_reason == "completed", (thread.ended_reason, thread.ended_detail)
    # The page says three judged passes: the begin node and each researcher / fact-checker / verdict pass are one superstep each, so a cap of 10 is
    # reached right after the third verdict, before a fourth pass starts that nothing would judge.
    one_pass = ["researcher", "fact-checker", "verdict"]
    assert order == one_pass * 3 + ["writer"]
    assert verdict([], [A]) in models["writer"].last_input(), "the writer is given the last verdict"


@pytest.mark.asyncio
async def test_raising_the_cap_by_three_allows_one_more_judged_pass(fake_storage_provider) -> None:
    """The page says to raise the cap in steps of three (13, 16, ...)."""
    thread, _, order = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["bad"], "verdict": [verdict([], [A])], "writer": ["# Report"]},
        max_iterations=13,
    )

    assert thread.ended_reason == "completed", (thread.ended_reason, thread.ended_detail)
    assert order == ["researcher", "fact-checker", "verdict"] * 4 + ["writer"]


@pytest.mark.asyncio
async def test_without_on_max_iterations_the_cap_ends_the_run_failed(fake_storage_provider) -> None:
    """The page says to drop on_max_iterations to get a failed run instead of an under-sourced report."""
    assert _cookbook_graph().on_max_iterations == "writer", "the page's graph should land on the writer at the cap"

    thread, _, order = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["bad"], "verdict": [verdict([], [A])], "writer": ["# Report"]},
        land_on_writer=False,
    )

    assert thread.ended_reason == "failed" and thread.ended_detail == "max_iterations_exceeded", (thread.ended_reason, thread.ended_detail)
    assert "writer" not in order


@pytest.mark.asyncio
async def test_a_verdict_wrapped_in_one_code_fence_still_routes(fake_storage_provider) -> None:
    fenced = "```json\n" + verdict([A], [B]) + "\n```"
    _, _, order = await _run(
        fake_storage_provider,
        {"researcher": [A, C], "fact-checker": ["x"], "verdict": [fenced, verdict([A, C], [])], "writer": ["# Report"]},
    )

    assert order == ["researcher", "fact-checker", "verdict"] * 2 + ["writer"]


@pytest.mark.asyncio
async def test_a_graph_input_without_a_question_fails_the_run_before_any_agent_answers(fake_storage_provider) -> None:
    """The page says the templates read initial_input.question and that a graph_input without it fails the node."""
    thread, models, _ = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["ok"], "verdict": [verdict([A], [])], "writer": ["# Report"]},
        graph_input={"topic": "SLOs"},
    )

    assert thread.ended_reason == "failed", (thread.ended_reason, thread.ended_detail)
    assert not models["researcher"].calls
    # The reason is on the failed node, and it is the template's own: the input has no `question`, not some other failure of the run.
    state = thread.node_states["researcher"]
    assert state.status.value == "failed"
    assert state.error and "render error" in state.error and "question" in state.error, state.error


@pytest.mark.asyncio
async def test_a_loop_that_loses_its_bound_is_stopped_by_the_test_not_left_to_hang(fake_storage_provider) -> None:
    """The page's graph with its cap removed would loop forever on an always-failing verdict. The scripted agents refuse to be called without end, so a
    page that loses max_iterations fails these tests quickly instead of hanging the lane."""
    thread, models, _ = await _run(
        fake_storage_provider,
        {"researcher": [A], "fact-checker": ["bad"], "verdict": [verdict([], [A])], "writer": ["# Report"]},
        unbounded=True,
    )

    assert thread.ended_reason == "failed", (thread.ended_reason, thread.ended_detail)
    assert len(models["researcher"].calls) == _RUNAWAY_CALLS


def test_the_default_input_template_fails_on_a_dict_graph_input() -> None:
    """The page says the default template walks the input as a list of messages and fails on a dict like the page's graph_input, which is why every
    node of the cookbook graph has its own template."""
    from primer.graph.template import render_input_template
    from primer.model.except_ import BadRequestError
    from primer.model.graph import _DEFAULT_INPUT_TEMPLATE, GraphContext

    context = GraphContext(initial_input={"question": "q"}, iteration=0, nodes={})

    with pytest.raises(BadRequestError):
        render_input_template(_DEFAULT_INPUT_TEMPLATE, context=context)
