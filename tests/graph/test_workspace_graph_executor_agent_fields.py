"""The workspace graph executor's agent resolver must not drop Agent fields either.

``WorkspaceGraphExecutor._wrap_agent_resolver`` appends the workspace's ``system_prompt_fragment`` to every
node agent. It rebuilt each agent from seven named fields (not even ``max_output_tokens``), so an
operator-set ``max_tool_turns``, ``max_output_tokens`` and the rest silently reset to their defaults for
every node of a workspace-bound graph: the same defect as ``WorkspaceAgentExecutor``'s, found alongside it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta
from primer.model.graph import Graph, _AgentNodeRef, _BeginNode, _EndNode, _GraphNodeRef, _StaticEdge
from primer.model_profile import ResolvedModel
from primer.model.model_profile import ModelProfileConfig
from primer.workspace.instructions import workspace_instructions_part
from tests.graph.test_workspace_executor import _FakeLLM, _make_state_repo

FRAGMENT = "WORKSPACE-FRAGMENT"


def _non_default_agent() -> Agent:
    return Agent(
        id="node-agent",
        description="a node agent with every knob turned",
        model=AgentModel(profile_id="p--m"),
        temperature=0.35,
        max_tool_turns=3,
        max_output_tokens=777,
        tools=["toolset__a"],
        system_prompt=["base prompt"],
        compaction_prompt=["summarise carefully"],
        compaction_tool_access=True,
        allow_external_tools=True,
        response_format={"type": "object"},
        tts_voice="alloy",
        harness_id="harness-1",
    )


async def _resolved(agent: Agent) -> Agent:
    async def base(_agent_id: str) -> Agent:
        return agent

    session = SimpleNamespace(system_prompt_fragment=FRAGMENT)
    resolve = WorkspaceGraphExecutor._wrap_agent_resolver(base, session)  # type: ignore[arg-type]
    return await resolve(agent.id)


async def test_every_agent_field_except_the_system_prompt_survives_the_wrapper() -> None:
    agent = _non_default_agent()

    seen = await _resolved(agent)

    dropped = [
        name for name in Agent.model_fields
        if name != "system_prompt" and getattr(seen, name) != getattr(agent, name)
    ]
    assert dropped == [], f"the graph executor's agent resolver dropped Agent fields: {dropped}"


async def test_the_system_prompt_gains_the_fragment_and_the_original_is_untouched() -> None:
    agent = _non_default_agent()

    seen = await _resolved(agent)

    assert seen.system_prompt == ["base prompt", FRAGMENT]
    assert agent.system_prompt == ["base prompt"]


async def test_the_resolved_agent_shares_no_mutable_list_with_the_original() -> None:
    agent = _non_default_agent()

    (await _resolved(agent)).tools.append("MUTATED")

    assert agent.tools == ["toolset__a"]


async def test_without_a_workspace_session_the_resolver_is_unchanged() -> None:
    sentinel = object()

    async def base(_agent_id: str):
        return sentinel

    assert WorkspaceGraphExecutor._wrap_agent_resolver(base, None) is base  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The workspace's agents.md (pivot P2, Task 8): every node agent gets it after the fragment
# ---------------------------------------------------------------------------


def _fragment_session() -> SimpleNamespace:
    return SimpleNamespace(system_prompt_fragment=FRAGMENT)


async def _resolve_with(agent: Agent, instructions: str | None, session=...) -> Agent:
    async def base(_agent_id: str) -> Agent:
        return agent

    session = _fragment_session() if session is ... else session
    resolve = WorkspaceGraphExecutor._wrap_agent_resolver(base, session, instructions)  # type: ignore[arg-type]
    return await resolve(agent.id)


async def test_node_agents_get_the_part_after_the_fragment() -> None:
    seen = await _resolve_with(_non_default_agent(), "rules")

    assert seen.system_prompt == ["base prompt", FRAGMENT, workspace_instructions_part("rules")]


async def test_every_other_agent_field_survives_when_instructions_are_added() -> None:
    agent = _non_default_agent()

    seen = await _resolve_with(agent, "rules")

    dropped = [
        name for name in Agent.model_fields
        if name != "system_prompt" and getattr(seen, name) != getattr(agent, name)
    ]
    assert dropped == [], f"the graph executor's agent resolver dropped Agent fields: {dropped}"
    assert agent.system_prompt == ["base prompt"], "the registry entity is not changed"


async def test_without_instructions_the_prompt_is_what_it_was() -> None:
    assert (await _resolve_with(_non_default_agent(), None)).system_prompt == ["base prompt", FRAGMENT]


async def test_a_blank_file_adds_no_part() -> None:
    assert (await _resolve_with(_non_default_agent(), " \n")).system_prompt == ["base prompt", FRAGMENT]


async def test_a_graph_with_no_holder_session_still_gets_the_instructions() -> None:
    """A legacy graph session has no holder (no fragment); the workspace's instructions still apply to its agents."""
    seen = await _resolve_with(_non_default_agent(), "rules", session=None)

    assert seen.system_prompt == ["base prompt", workspace_instructions_part("rules")]


async def test_no_session_and_no_file_leaves_the_agent_the_resolver_returned() -> None:
    agent = _non_default_agent()

    assert await _resolve_with(agent, "  ", session=None) is agent

    async def base(_agent_id: str):
        return agent

    assert WorkspaceGraphExecutor._wrap_agent_resolver(base, None, None) is base  # type: ignore[arg-type]


def _one_agent_graph(graph_id: str) -> Graph:
    return Graph(
        id=graph_id,
        description="Begin -> Agent -> End",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="A", agent_id="x", input_template="go"),
            _EndNode(id="end", output_template="{{ nodes.A.text }}"),
        ],
        edges=[_StaticEdge(from_node="begin", to_node="A"), _StaticEdge(from_node="A", to_node="end")],
    )


class _FakeWorkspaceSession:
    workspace_id = "ws-test"
    session_id = "sess-test"
    agent_id = "agent-fake"
    system_prompt_fragment = "<<FRAGMENT>>"

    def __init__(self) -> None:
        self.workspace_tools: list = []


def _executor(graph: Graph, repo, llm: _FakeLLM, *, instructions: str | None, gsid: str) -> WorkspaceGraphExecutor:
    async def agent_resolver(agent_id: str) -> Agent:
        return Agent(id="x", description="x", model=AgentModel(profile_id="p--m"), system_prompt=["Base."])

    async def llm_resolver(_a: Agent):
        return (llm, ResolvedModel(
            profile_id="test-profile", provider_id="test-provider", model_name="m", context_length=128_000,
            config=ModelProfileConfig(),
        ))

    return WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,  # type: ignore[arg-type]
        state_repo=repo,
        graph_session_id=gsid,
        workspace_session=_FakeWorkspaceSession(),  # type: ignore[arg-type]
        graph_input="seed",
        workspace_instructions=instructions,
    )


async def test_a_node_agent_sends_the_file_to_the_model_as_written(tmp_path: Path) -> None:
    raw = "Use {{ nodes.A.text }} and {% tags %} as plain text."
    repo = await _make_state_repo(tmp_path, workspace_id="ws-test")
    llm = _FakeLLM(scripts=[[TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]])

    executor = _executor(_one_agent_graph("g-instr"), repo, llm, instructions=raw, gsid="gsid-instr")
    async for _ in executor.invoke([]):
        pass

    system_texts = [m.parts[0].text for call in llm.calls for m in call["messages"] if m.role == "system"]
    assert system_texts == ["Base.\n\n<<FRAGMENT>>\n\n## Workspace instructions (agents.md)\n\n" + raw]


async def test_a_subgraph_child_inherits_the_instructions(tmp_path: Path) -> None:
    """A subgraph node's agents run in the same workspace: the child executor is built from the parent's raw resolver,
    so it has to be handed the file too or every node below the first level would run without it."""
    sub_node = _GraphNodeRef(id="SUB", graph_id="inner")
    parent_graph = Graph(
        id="g-parent-instr",
        description="begin -> sub -> end",
        nodes=[_BeginNode(id="begin"), sub_node, _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="SUB"), _StaticEdge(from_node="SUB", to_node="end")],
    )
    repo = await _make_state_repo(tmp_path, workspace_id="ws-test")
    parent = _executor(parent_graph, repo, _FakeLLM(scripts=[]), instructions="rules", gsid="gsid-parent-instr")

    child = await parent._build_sub_executor(sub_node, _one_agent_graph("g-inner-instr"))

    seen = await child._agent_resolver("x")
    assert seen.system_prompt == ["Base.", "<<FRAGMENT>>", workspace_instructions_part("rules")]
