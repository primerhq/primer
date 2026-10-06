"""What ends a SESSION turn: one predicate for every reader of ``messages.jsonl`` (ticket 01a11232).

A subagent's records are written inline into the parent session's log and carry ``payload.delegated``. Each delegated
run ends with a ``done`` (or an ``error`` / ``cancelled``) of its own, and that record is the END OF THE SUBAGENT'S TURN,
never of the session's. Three readers used to decide "this record ends a turn" on their own and none asked whether it
was delegated: the timeline's turn windows (and the trace's turn ordinals), the open-turn count the completed-turn guard
uses, and the final-result relay's boundary scan. A leaf module (it needs only the record kinds) so all three, and the
console's mirror of it, can share the one rule.

Two questions, deliberately separate:

* :func:`is_session_terminal` - is this record a terminal of the SESSION's own run (not a subagent's)? A model call that
  ended in a tool call is one: it ends a round, and the relay still posts the text after the last round.
* :func:`closes_turn` - does it end the user's TURN? That is a session terminal that is not a tool round's ``done``.
"""

from __future__ import annotations

from typing import Any

from primer.model.workspace_session import SessionMessageKind

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value

TERMINAL_KINDS = frozenset({_DONE, _ERROR, _CANCELLED})


def is_delegated(rec: dict[str, Any]) -> bool:
    """True when ``rec`` was written by a delegated (subagent) run rather than by the session's own turn."""
    return bool((rec.get("payload") or {}).get("delegated"))


def is_session_terminal(rec: dict[str, Any]) -> bool:
    """True when ``rec`` is a ``done`` / ``error`` / ``cancelled`` of the session's OWN run.

    A delegated run's terminal is the subagent's turn end, never the session's, so it is not one.
    """
    return rec.get("kind") in TERMINAL_KINDS and not is_delegated(rec)


def closes_turn(rec: dict[str, Any]) -> bool:
    """True when ``rec`` ends the session's turn rather than a tool round or a subagent's run.

    The agent loop issues one ``llm.stream`` call per tool round and every stream ends with its own ``Done``
    (primer/agent/loop.py), so an intermediate round produces a DONE record carrying ``stop_reason="tool_use"``.
    Counting those as terminals would split one turn into several windows. A delegated run's terminal is not the
    session's (see :func:`is_session_terminal`).
    """
    if not is_session_terminal(rec):
        return False
    if rec.get("kind") == _DONE:
        return (rec.get("payload") or {}).get("stop_reason") != "tool_use"
    return True


__all__ = ["TERMINAL_KINDS", "closes_turn", "is_delegated", "is_session_terminal"]
