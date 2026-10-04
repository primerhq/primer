"""The workspace graph executor's agent resolver must not drop Agent fields either.

``WorkspaceGraphExecutor._wrap_agent_resolver`` appends the workspace's ``system_prompt_fragment`` to every
node agent. It rebuilt each agent from seven named fields (not even ``max_output_tokens``), so an
operator-set ``max_tool_turns``, ``max_output_tokens`` and the rest silently reset to their defaults for
every node of a workspace-bound graph: the same defect as ``WorkspaceAgentExecutor``'s, found alongside it.
"""

from __future__ import annotations

from types import SimpleNamespace

from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.agent import Agent, AgentModel

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
