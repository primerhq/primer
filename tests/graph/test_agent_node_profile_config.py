"""A graph agent node's model calls apply the profile's config.

A graph agent node runs its turn through ``run_agent_turn`` with the
``(LLM, ResolvedModel)`` pair its ``llm_resolver`` returns, so the profile it
resolved (the agent's own, or the node's ``profile_id`` override) decides the
reasoning settings of every call the node makes. Reuses the graph test
helpers from ``tests.graph.test_workspace_executor`` and the recording fake
adapter from ``tests.agent.test_profile_config_call_sites``.
"""

from __future__ import annotations

from pathlib import Path

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _StaticEdge
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel
from primer.model_profile import ResolvedModel
from tests.agent.test_profile_config_call_sites import RecordingLLM
from tests.graph.test_workspace_executor import _drain, _make_state_repo

_PROFILES = {
    "p--m": ModelProfileConfig(reasoning=ReasoningLevel.OFF),
    "p--think": ModelProfileConfig(reasoning=ReasoningLevel.HIGH, extended={"top_k": 20}),
}


def _graph(node: _AgentNodeRef) -> Graph:
    return Graph(
        id="g-profile-config",
        description="Begin -> Agent -> End",
        nodes=[_BeginNode(id="begin"), node, _EndNode(id="end")],
        edges=[
            _StaticEdge(from_node="begin", to_node=node.id),
            _StaticEdge(from_node=node.id, to_node="end"),
        ],
    )


async def _run_node(tmp_path: Path, node: _AgentNodeRef) -> RecordingLLM:
    llm = RecordingLLM([[TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]])
    agent = Agent(id="x", description="x", model=AgentModel(profile_id="p--m"))

    async def agent_resolver(agent_id: str) -> Agent:
        return agent

    async def llm_resolver(agent: Agent, profile_id: str | None = None):
        chosen = profile_id or agent.model.profile_id
        return llm, ResolvedModel(
            profile_id=chosen, provider_id="prov", model_name="m",
            context_length=128_000, config=_PROFILES[chosen],
        )

    executor = WorkspaceGraphExecutor(
        graph=_graph(node),
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=await _make_state_repo(tmp_path, workspace_id="ws-profile-config"),
        graph_session_id="gsid-profile-config",
    )
    await _drain(executor.invoke([]))
    return llm


async def test_a_node_call_carries_the_agent_profile_config(tmp_path: Path) -> None:
    llm = await _run_node(tmp_path, _AgentNodeRef(id="A", agent_id="x", input_template="go"))
    assert len(llm.calls) == 1
    assert llm.calls[0]["extended"] == {"think": False}


async def test_a_node_profile_override_brings_its_own_config(tmp_path: Path) -> None:
    """One agent, a reasoning profile in this node: the node's profile wins."""
    llm = await _run_node(
        tmp_path,
        _AgentNodeRef(id="A", agent_id="x", input_template="go", profile_id="p--think"),
    )
    assert llm.calls[0]["extended"] == {"top_k": 20, "think": True}
