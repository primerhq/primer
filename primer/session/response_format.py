"""Structured-output precedence for a session turn.

Three sources can ask for a JSON Schema, and they are not equal. An
ephemeral value supplied with one steer beats the session's persistent
setting, which beats the agent's default. That ordering lets a caller
ask a single turn for structured output without disturbing the
session, and lets a session opt in without editing the agent.

The ephemeral value is POPPED rather than read, inside the same lock
section that flips turn_status, so exactly one turn ever sees it even
if that turn is retried.
"""

from __future__ import annotations

from typing import Any

from primer.model.workspace_session import WorkspaceSession

EPHEMERAL_KEY = "ephemeral_response_format"


def effective_response_format(
    row: WorkspaceSession, *, agent_default: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve which schema this turn should use."""
    ephemeral = (row.metadata or {}).get(EPHEMERAL_KEY)
    if ephemeral is not None:
        return ephemeral
    if row.response_format is not None:
        return row.response_format
    return agent_default


def pop_ephemeral(row: WorkspaceSession) -> dict[str, Any] | None:
    """Take the one-turn schema off the row, mutating its metadata.

    Popping is what bounds the value to a single turn: a retry of the
    same turn finds nothing left and falls back to the session or agent
    setting rather than silently re-applying it.
    """
    if not row.metadata:
        return None
    return row.metadata.pop(EPHEMERAL_KEY, None)


__all__ = ["EPHEMERAL_KEY", "effective_response_format", "pop_ephemeral"]
