"""WorkspaceAgentExecutor must not drop Agent fields when it extends the system prompt.

It used to rebuild the agent from eight named fields, so every other field fell back to its default:
an operator-set ``max_tool_turns`` (3, or 200) became 50 and ``compaction_tool_access`` became False in
every workspace session, and nothing reported it. The executor now copies the agent and replaces only
``system_prompt``. These tests pin that, and make a NEW Agent field impossible to drop silently.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel

FRAGMENT = "WORKSPACE-FRAGMENT"


def _non_default_agent() -> Agent:
    """An agent with EVERY field set to something other than its default."""
    return Agent(
        id="ag-fields",
        description="an agent with every knob turned",
        model=AgentModel(profile_id="p--m"),
        temperature=0.35,
        max_tool_turns=3,
        max_output_tokens=777,
        tools=["toolset__a", "toolset__b"],
        system_prompt=["base prompt"],
        compaction_prompt=["summarise carefully"],
        compaction_tool_access=True,
        allow_external_tools=True,
        response_format={"type": "object", "properties": {"x": {"type": "string"}}},
        tts_voice="alloy",
        harness_id="harness-1",
    )


def _executor(agent: Agent) -> WorkspaceAgentExecutor:
    session = SimpleNamespace(
        system_prompt_fragment=FRAGMENT, workspace_id="ws-1", session_id="s-1",
    )
    return WorkspaceAgentExecutor(
        agent=agent, llm=object(), llm_model=object(), tool_manager=object(),  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
    )


def _default_of(name: str):
    field = Agent.model_fields[name]
    return None if field.is_required() else field.get_default(call_default_factory=True)


def test_the_fixture_sets_every_agent_field_to_a_non_default_value() -> None:
    """The guard that makes the round-trip test below meaningful: if someone adds an Agent field and
    does not set it here, a dropped value would compare equal to the default and slip through."""
    agent = _non_default_agent()
    untouched = [
        name for name in Agent.model_fields
        if not Agent.model_fields[name].is_required()
        and getattr(agent, name) == _default_of(name)
    ]
    assert untouched == [], (
        f"set these Agent fields to a non-default value in _non_default_agent(): {untouched}"
    )


def test_every_agent_field_except_the_system_prompt_survives_the_executor() -> None:
    agent = _non_default_agent()

    seen = _executor(agent)._agent

    dropped = [
        name for name in Agent.model_fields
        if name != "system_prompt" and getattr(seen, name) != getattr(agent, name)
    ]
    assert dropped == [], f"the executor dropped Agent fields: {dropped}"


def test_an_operator_set_max_tool_turns_reaches_the_loop_and_the_summariser() -> None:
    executor = _executor(_non_default_agent())

    assert executor._agent.max_tool_turns == 3
    assert executor._compaction_tool_kwargs()["max_tool_turns"] == 3


def test_compaction_tool_access_is_honoured() -> None:
    assert "tool_manager" in _executor(_non_default_agent())._compaction_tool_kwargs()
    plain = _non_default_agent().model_copy(update={"compaction_tool_access": False})
    assert _executor(plain)._compaction_tool_kwargs() == {}


def test_the_system_prompt_gains_the_workspace_fragment_and_nothing_else_changes() -> None:
    agent = _non_default_agent()

    seen = _executor(agent)._agent

    assert seen.system_prompt == ["base prompt", FRAGMENT]
    assert agent.system_prompt == ["base prompt"], "the original agent definition must stay unchanged"


@pytest.mark.parametrize("field", ["tools", "compaction_prompt", "system_prompt"])
def test_the_executors_agent_shares_no_mutable_list_with_the_original(field: str) -> None:
    """The original is a registry entity: mutating the executor's copy must not reach it."""
    agent = _non_default_agent()
    before = list(getattr(agent, field))

    getattr(_executor(agent)._agent, field).append("MUTATED-BY-THE-EXECUTOR")

    assert getattr(agent, field) == before
