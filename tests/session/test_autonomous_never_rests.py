"""AG-M4: an autonomous session never rests WAITING after a turn.

A trigger or webhook one-shot (or a graph) has no person to answer it, and a WAITING row with ``turn_no >= 1`` holds a
``parallelism="skip"`` gate shut for ever. The executor-set WAITING (the answer ended with "?") and the ``max_tokens`` /
``content_filter`` rests used to slip past the autonomous exemption.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import _post_turn_status
from primer.trigger.subscribers import session_holds_skip_gate


@pytest.mark.parametrize("reason,agent_status", [
    ("stop", SessionStatus.WAITING),        # the answer ended with "?": the executor set WAITING
    ("end_turn", SessionStatus.WAITING),
    (None, SessionStatus.WAITING),
    ("max_tokens", None),
    ("max_tokens", SessionStatus.RUNNING),
    ("content_filter", None),
    ("content_filter", SessionStatus.RUNNING),
])
def test_autonomous_turn_ends_completed_instead_of_resting(reason, agent_status):
    status, ended = _post_turn_status(reason, agent_status, autonomous=True)
    assert (status, ended) == (SessionStatus.ENDED, "completed")
    row = WorkspaceSession(id="s", workspace_id="w", binding=AgentSessionBinding(agent_id="a"), status=status,
                           created_at=datetime.now(timezone.utc), turn_no=1, autonomous=True)
    assert session_holds_skip_gate(row) is False


@pytest.mark.parametrize("reason", ["stop", "end_turn", "stop_sequence", "max_tokens", "content_filter", "tool_turn_cap", None, "x"])
@pytest.mark.parametrize("agent_status", [None, SessionStatus.WAITING, SessionStatus.RUNNING, SessionStatus.PAUSED, SessionStatus.ENDED])
def test_autonomous_never_returns_waiting(reason, agent_status):
    assert _post_turn_status(reason, agent_status, autonomous=True)[0] != SessionStatus.WAITING
