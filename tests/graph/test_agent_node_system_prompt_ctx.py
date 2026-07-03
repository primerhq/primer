"""A graph agent-node renders its agent's system_prompt against context.ctx,
so a workspace run includes the {% if ctx.surface == 'workspace' %} block.

Reuses the capturing _FakeLLM (records stream() messages) + _make_state_repo
from tests.graph.test_workspace_executor."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import LLMModel
from tests.graph.test_workspace_executor import _FakeLLM, _make_state_repo

_WS_BLOCK = (
    "Base.\n\n{% if ctx.surface == 'workspace' %}"
    "USE-FILES {{ ctx.artifact_dir }}{% endif %}"
)


class _FakeWorkspaceSession:
    workspace_id = "ws-test"
    session_id = "sess-test"
    agent_id = "agent-fake"
    system_prompt_fragment = "<<FRAGMENT>>"

    def __init__(self) -> None:
        self.workspace_tools: list = []


async def _drain(it) -> list[StreamEvent]:
    return [ev async for ev in it]


@pytest.mark.asyncio
async def test_graph_agent_node_includes_workspace_block(tmp_path: Path) -> None:
    graph = Graph(
        id="g-anode-ctx",
        description="Begin -> Agent -> End",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x", input_template="go"),
            _EndNode(id="end", output_template="{{ nodes.A.text }}"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="A"),
            _StaticEdge(from_node="A", to_node="end"),
        ],
    )
    repo = await _make_state_repo(tmp_path, workspace_id="ws-test")
    llm = _FakeLLM(
        scripts=[
            [
                TextDelta(text="ok", index=0),
                Done(stop_reason="stop", raw_reason="stop"),
            ]
        ]
    )

    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(
            id="x",
            description="x",
            model=AgentModel(provider_id="p", model_name="m"),
            system_prompt=[_WS_BLOCK],
        )

    async def llm_resolver(_a: Agent):
        return (llm, LLMModel(name="m", context_length=128_000))

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id="gsid-anode",
        workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
        graph_input="seed",
    )
    await _drain(executor.invoke([]))

    sys_texts: list[str] = []
    for call in llm.calls:
        for m in call["messages"]:
            if getattr(m, "role", None) == "system":
                sys_texts.append(m.parts[0].text)
    joined = "\n".join(sys_texts)
    assert "USE-FILES artifacts/gsid-anode" in joined
