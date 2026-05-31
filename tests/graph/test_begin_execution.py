"""Begin execution integration tests (spec §2.1 / §7.3).

Drives a Begin -> Agent -> End graph through the full
:class:`WorkspaceGraphExecutor` for each accepted input shape (string,
``list[Message]``, dict). The downstream agent's ``input_template``
reads either ``{{ nodes.begin.parsed.q }}`` (dict input → parsed
populated) or ``{{ nodes.begin.text }}`` (string / message-list input)
to assert the materialised :class:`NodeOutput` is wired into Jinja
context as the spec requires.

Phase 3's :file:`test_begin_firing.py` already covers
``_materialise_begin_output`` as a unit. This file is the integration
variant: the full executor runs and we observe what the agent stub
actually received in its prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, StreamEvent, TextDelta, TextPart
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from primer.workspace.local.state import LocalStateRepo as StateRepo


class _CapturingFakeLLM:
    """Records every ``stream`` call so the test can inspect the user
    message the downstream agent saw."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append(kwargs)
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="ack", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


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


async def _drain(it):
    return [ev async for ev in it]


def _begin_agent_end_graph(
    *,
    template: str,
    graph_id: str = "g-be",
) -> Graph:
    """Begin -> agent (reading the provided template) -> End."""
    return Graph(
        id=graph_id,
        description="Begin -> agent -> End",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(
                id="a",
                agent_id="ag",
                input_template=template,
            ),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="a"),
            _StaticEdge(from_node="a", to_node="end"),
        ],
    )


async def _run(
    *, graph: Graph, tmp_path: Path, graph_input: Any = None,
    invoke_messages: list[Message] | None = None,
) -> _CapturingFakeLLM:
    repo = await _make_state_repo(tmp_path)
    llm = _CapturingFakeLLM()

    async def agent_resolver(agent_id: str) -> Agent:
        return _agent(agent_id)

    async def llm_resolver(_a: Agent):
        return (llm, _model())

    kwargs: dict[str, Any] = dict(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=f"gsid-{graph.id}",
    )
    if graph_input is not None:
        kwargs["graph_input"] = graph_input
    executor = WorkspaceGraphExecutor(**kwargs)
    await _drain(executor.invoke(invoke_messages or []))
    return llm


def _last_user_text(llm: _CapturingFakeLLM) -> str:
    """Pull the last user-role text out of the most recent stream call."""
    assert llm.calls, "expected the agent stub to be invoked at least once"
    messages = llm.calls[-1]["messages"]
    # Most recent user message — the Jinja-rendered template.
    user_msgs = [m for m in messages if m.role == "user"]
    assert user_msgs, "expected a user message in the agent's prompt"
    parts = user_msgs[-1].parts
    return "".join(getattr(p, "text", "") for p in parts)


# ===========================================================================
# Each input shape reaches the downstream agent via the correct template
# variable.
# ===========================================================================


@pytest.mark.asyncio
async def test_dict_input_populates_parsed_reachable_via_template(
    tmp_path: Path,
) -> None:
    """A dict ``graph_input`` lands in ``nodes.begin.parsed`` and the
    downstream agent's Jinja template can read individual keys."""
    graph = _begin_agent_end_graph(
        template="hello {{ nodes.begin.parsed.q }}",
        graph_id="g-begin-dict",
    )
    llm = await _run(
        graph=graph,
        tmp_path=tmp_path,
        graph_input={"q": "world"},
    )
    assert "hello world" in _last_user_text(llm)


@pytest.mark.asyncio
async def test_string_input_reachable_via_nodes_begin_text(
    tmp_path: Path,
) -> None:
    """A string ``graph_input`` lands in ``nodes.begin.text`` (parsed
    remains ``None``); the downstream agent reads it as plain text."""
    graph = _begin_agent_end_graph(
        template="echo: {{ nodes.begin.text }}",
        graph_id="g-begin-str",
    )
    llm = await _run(
        graph=graph,
        tmp_path=tmp_path,
        graph_input="research X",
    )
    assert "echo: research X" in _last_user_text(llm)


@pytest.mark.asyncio
async def test_message_list_input_concatenates_into_nodes_begin_text(
    tmp_path: Path,
) -> None:
    """A ``list[Message]`` initial input (no ``graph_input``) materialises
    Begin's text by concatenating each message's first text part."""
    graph = _begin_agent_end_graph(
        template="from-begin: {{ nodes.begin.text }}",
        graph_id="g-begin-msglist",
    )
    msgs = [
        Message(role="user", parts=[TextPart(text="part-one")]),
        Message(role="user", parts=[TextPart(text="part-two")]),
    ]
    llm = await _run(
        graph=graph,
        tmp_path=tmp_path,
        invoke_messages=msgs,
    )
    seen = _last_user_text(llm)
    assert "from-begin:" in seen
    assert "part-one" in seen
    assert "part-two" in seen
