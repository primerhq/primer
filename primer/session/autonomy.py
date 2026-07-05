"""Derive whether a session is autonomous (self-driving) or interactive.

Autonomous sessions (graphs, agent loops) get Pause/Cancel controls and END
after a clean turn; interactive sessions (agents awaiting a human) get
Stop/End and STAY ALIVE after a clean turn so the user can keep chatting.
See docs/superpowers/reqs/studio-agents-interact.md §4.4 / §8.1.
"""

from __future__ import annotations

from primer.model.workspace_session import (
    GraphSessionBinding,
    WorkspaceSession,
)


def session_is_autonomous(session: WorkspaceSession) -> bool:
    """True when the session should be treated as self-driving.

    Explicit ``session.autonomous`` wins; otherwise derive from binding
    kind (graph ⇒ autonomous, agent ⇒ interactive).
    """
    if session.autonomous is not None:
        return session.autonomous
    return isinstance(session.binding, GraphSessionBinding)


__all__ = ["session_is_autonomous"]
