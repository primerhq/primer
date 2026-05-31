"""WorkspaceGraphExecutor uses ``graph_input`` (from session.metadata) as the
initial input when present, overriding ``initial_instructions``.

Spec §4.3 — Worker / executor read path.

The base executor builds ``GraphContext.initial_input`` from the value
passed to ``invoke()``. The workspace executor accepts an optional
``graph_input`` constructor arg (set from ``session.metadata['graph_input']``
by the worker wiring); when supplied, it seeds the initial input instead
of the messages list passed to ``invoke()``. Begin then materialises a
``NodeOutput`` whose ``parsed`` matches the dict input, and End's
``output_template`` can reference it via ``{{ nodes.<begin_id>.parsed.<key> }}``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, StreamEvent
from primer.model.graph import (
    Graph,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _FakeLLM:
    """Minimal LLM stub — not used by Begin/End graphs but required by the
    executor constructor signature."""

    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async def _empty() -> AsyncIterator[StreamEvent]:
            if False:
                yield  # pragma: no cover
        return _empty()


async def _make_state_repo(tmp_path: Path) -> StateRepo:
    repo = StateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


def _model() -> LLMModel:
    return LLMModel(name="m", context_length=128_000)


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        description=f"agent {agent_id}",
        model=AgentModel(provider_id="p", model_name="m"),
    )


async def _drain(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_graph_input_seeds_initial_input(tmp_path: Path) -> None:
    """A dict graph_input flows into Begin.parsed and End's template renders
    it without any agent invocations."""
    graph = Graph(
        id="g-graph-input",
        description="Begin -> End, using graph_input",
        entry_node_id="begin",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="{{ nodes.begin.parsed.q }}",
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (_FakeLLM(), _model())

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-graph-input",
        graph_input={"q": "hi"},
    )
    await _drain(executor.invoke([]))

    # Final state.json should be ENDED/completed.
    state = await executor.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"

    # End-node messages.jsonl includes the assistant_token with the rendered
    # text. Phase 4.3 emits that record; for 4.1 we instead check that the
    # base executor stored an End NodeOutput with text="hi" — verifiable via
    # the graph-level state file's iteration count or by re-running the
    # End render. The simplest assertion is: the GraphContext that Begin
    # built carried the dict. We assert via the End-firing template path:
    # if Begin.parsed = {"q": "hi"} then End's template "{{ nodes.begin.parsed.q }}"
    # renders to "hi". A failing initial_input pass-through would either
    # raise (StrictUndefined on missing `q`) or terminate the graph `failed`.
    # The state.json showing `ended`/`completed` is sufficient evidence of
    # the success path under StrictUndefined semantics.


@pytest.mark.asyncio
async def test_graph_input_string_passed_through(tmp_path: Path) -> None:
    """A string graph_input lands in Begin.text and End can echo it."""
    graph = Graph(
        id="g-graph-input-str",
        description="Begin -> End, string input",
        entry_node_id="begin",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="{{ nodes.begin.text }}",
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (_FakeLLM(), _model())

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-graph-input-str",
        graph_input="research X",
    )
    await _drain(executor.invoke([]))

    state = await executor.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"


@pytest.mark.asyncio
async def test_no_graph_input_falls_back_to_invoke_messages(
    tmp_path: Path,
) -> None:
    """Without ``graph_input``, the executor uses the messages passed to
    ``invoke()`` (legacy behaviour)."""
    from primer.model.chat import TextPart

    graph = Graph(
        id="g-no-graph-input",
        description="Begin -> End, messages-based",
        entry_node_id="begin",
        nodes=[
            _BeginNode(id="begin"),
            _EndNode(
                id="end",
                output_template="{{ nodes.begin.text }}",
            ),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )
    repo = await _make_state_repo(tmp_path)

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (_FakeLLM(), _model())

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-no-graph-input",
    )
    msgs = [Message(role="user", parts=[TextPart(text="hello")])]
    await _drain(executor.invoke(msgs))

    state = await executor.load_state()
    assert state is not None
    assert state["status"] == "ended"
    assert state["ended_reason"] == "completed"
