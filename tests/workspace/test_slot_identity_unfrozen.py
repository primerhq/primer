"""Session slot identity is no longer frozen (S1 P5 Task 30).

P3 made switching legal; this makes it durable. The slot on disk was
written once at create and its constructor rejected any later
disagreement, so a session that switched agents could fail to rehydrate
after a restart: the row said agent B, the slot still said agent A.

The DB row is sole truth for the binding (amendment M14). The slot's
agent_id survives as a historical artefact of how the session started,
not as an assertion about what it is running now.
"""

from datetime import UTC, datetime

from primer.model.workspace_session import AgentBinding, SessionInfo, SessionStatus
from primer.workspace.session import AgentSession


def _info(agent_id: str) -> SessionInfo:
    return SessionInfo(
        session_id="sess-1",
        agent_id=agent_id,
        workspace_id="ws-1",
        status=SessionStatus.RUNNING,
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )


def _binding(agent_id: str) -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name=agent_id)


class _Repo:
    pass


class _Cache:
    pass


def _session(info_agent: str, binding_agent: str) -> AgentSession:
    return AgentSession(
        session_info=_info(info_agent),
        agent_binding=_binding(binding_agent),
        state_repo=_Repo(),
        truncation_store=_Cache(),
    )


def test_matching_identity_still_constructs():
    session = _session("agent-a", "agent-a")
    assert session is not None


def test_a_switched_session_rehydrates_instead_of_raising():
    """The whole point: the row switched to agent-b while the slot on
    disk still records agent-a, and that must load."""
    session = _session("agent-a", "agent-b")
    assert session is not None


def test_a_graph_holder_slot_rehydrates_under_an_agent_binding():
    """A session switched from a graph to an agent has a graph-shaped
    slot and an agent-shaped row."""
    session = _session("graph:g-1", "agent-b")
    assert session is not None
