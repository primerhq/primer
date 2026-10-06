"""Inline subagent runs record into the delegating session transcript.

run_subagent and resume_subagent execute INSIDE the delegating turn and
own no writer, so until now a delegated run left no trace: the parent
transcript showed one opaque invoke_agent tool call and its final text,
and everything the subagent actually did was invisible.

The dispatch loop publishes a recorder through a contextvar and the
invoke loops feed it every subagent stream event. Attribution rides
payload["delegate_tool_call_id"], which is the anchor the trace view
and the transcript both nest on, plus a per-run id and depth
(payload["delegate_run_id"], ["delegate_parent_run_id"],
["delegate_depth"]) for the readers that must tell nested runs apart
when the raw call ids collide.

What the recorder writes are SessionMessageRecord event-log lines, so
they are excluded from prompt rebuilding by construction: the history
reader admits only role/parts Message lines
(primer/workspace/session.py). That is the property that makes this
safe. A delegated run becomes visible to readers without its chatter
being replayed back into the parent's next turn.
"""

from __future__ import annotations

import contextvars
from typing import Any

from primer.agent.call_scope import current_call_scope
from primer.session.persistence import _CoalesceState, translate_stream_event

_SINK: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "primer_delegation_sink", default=None,
)


def set_delegation_sink(sink: Any) -> contextvars.Token:
    """Publish a recorder for the duration of a turn."""
    return _SINK.set(sink)


def reset_delegation_sink(token: contextvars.Token) -> None:
    _SINK.reset(token)


def current_delegation_sink() -> Any | None:
    """The recorder for the turn on this task, if one is active."""
    return _SINK.get()


def _abandoned() -> bool:
    """Is the tool call this code runs inside abandoned (see :mod:`primer.agent.call_scope`)?"""
    scope = current_call_scope()
    return scope is not None and scope.abandoned


class DelegationRecorder:
    """Translate subagent stream events into parent-session records.

    Carries its own coalescing state so a subagent's text deltas
    accumulate independently of the parent turn's, rather than
    interleaving into one another's buffers.
    """

    def __init__(
        self, *, writer: Any, event_bus: Any, session_id: str, turn_no: int = 0,
    ) -> None:
        self._writer = writer
        self._bus = event_bus
        self._session_id = session_id
        self._turn_no = turn_no
        self._state = _CoalesceState()

    async def on_event(
        self,
        ev: Any,
        *,
        delegate_tool_call_id: str | None,
        delegate_run_id: str | None = None,
        delegate_parent_run_id: str | None = None,
        delegate_depth: int | None = None,
    ) -> None:
        """Record one event of a delegated run.

        ``delegate_tool_call_id`` is the delegating call's RAW provider id, which is not unique (providers that synthesise ids
        restart the numbering every stream, so a child's call and its parent's can be the same string). What tells two runs
        apart is ``delegate_run_id``, minted when the run starts and kept across a park/resume;
        ``delegate_parent_run_id`` is the run whose call delegated to this one (absent when the parent turn itself did), and
        ``delegate_depth`` the nesting depth (1 is a direct delegation of the parent turn, the number ``AgentFrame.depth``
        carries). Each is stamped only when given: a record written before this carried none, and the readers fall back to
        the call id.
        """
        # Stop slice B1: once a Stop has given up on the tool call this runs inside (or on a call that call is nested
        # in), nothing it emits may be appended after the call's "interrupted" answer, whatever delegate id the event
        # carries (a subagent that delegated tags its events with the INNER call's id). The call's scope is inherited by
        # everything the call started, so it is the one thing every such event can be asked.
        if _abandoned():
            return
        result = translate_stream_event(ev, self._state, turn_no=self._turn_no)
        if result is None:
            return  # coalesced or not persistable; most events land here
        records = result if isinstance(result, list) else [result]
        for rec in records:
            if _abandoned():
                return  # abandoned while an earlier record of this event was being written
            rec.payload["delegated"] = True
            rec.payload["delegate_tool_call_id"] = delegate_tool_call_id
            if delegate_run_id is not None:
                rec.payload["delegate_run_id"] = delegate_run_id
            if delegate_parent_run_id is not None:
                rec.payload["delegate_parent_run_id"] = delegate_parent_run_id
            if delegate_depth is not None:
                rec.payload["delegate_depth"] = delegate_depth
            seq = await self._writer.append(rec)
            await self._bus.publish(
                f"session:{self._session_id}:tick", {"seq": seq},
            )


__all__ = [
    "DelegationRecorder",
    "current_delegation_sink",
    "reset_delegation_sink",
    "set_delegation_sink",
]
