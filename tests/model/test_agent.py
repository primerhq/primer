"""Agent model tests (allow_external_tools flag + AgentBinding mirror)."""


def test_allow_external_tools_defaults_false():
    from primer.model.agent import Agent, AgentModel

    a = Agent(
        id="agent-x",
        description="d",
        model=AgentModel(profile_id="prof-1"),
    )
    assert a.allow_external_tools is False


def test_agent_binding_mirrors_allow_external_tools():
    from primer.model.workspace_session import AgentBinding

    b = AgentBinding(agent_id="agent-x", agent_name="X")
    assert b.allow_external_tools is False
    b2 = AgentBinding(
        agent_id="agent-x", agent_name="X", allow_external_tools=True
    )
    assert b2.allow_external_tools is True
