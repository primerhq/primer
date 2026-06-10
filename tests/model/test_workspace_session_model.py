"""WorkspaceSession: additive parked_event_keys for multi-event parks."""
from datetime import datetime, timezone
from primer.model.workspace_session import (
    AgentSessionBinding, SessionStatus, WorkspaceSession,
)


def _sess(**kw):
    return WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc), **kw,
    )


def test_parked_event_keys_defaults_none():
    assert _sess().parked_event_keys is None


def test_parked_event_keys_roundtrips():
    s = _sess(parked_event_keys=["k1", "k2"])
    back = WorkspaceSession.model_validate(s.model_dump())
    assert back.parked_event_keys == ["k1", "k2"]
