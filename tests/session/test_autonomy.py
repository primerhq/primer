from datetime import datetime, timezone

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.autonomy import session_is_autonomous


def _mk(binding, autonomous=None):
    return WorkspaceSession(
        id="sess-x",
        workspace_id="ws-1",
        binding=binding,
        status=SessionStatus.CREATED,
        autonomous=autonomous,
        created_at=datetime.now(timezone.utc),
    )


def test_graph_binding_defaults_autonomous():
    s = _mk(GraphSessionBinding(graph_id="g1"))
    assert session_is_autonomous(s) is True


def test_agent_binding_defaults_interactive():
    s = _mk(AgentSessionBinding(agent_id="a1"))
    assert session_is_autonomous(s) is False


def test_explicit_flag_overrides_binding():
    # An agent running a self-driving loop is marked autonomous at create.
    s = _mk(AgentSessionBinding(agent_id="a1"), autonomous=True)
    assert session_is_autonomous(s) is True
    # A graph explicitly forced interactive (rare) honours the flag.
    g = _mk(GraphSessionBinding(graph_id="g1"), autonomous=False)
    assert session_is_autonomous(g) is False
