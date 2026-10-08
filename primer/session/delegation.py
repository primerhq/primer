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
import logging
from typing import Any

from primer.agent.call_scope import current_call_scope
from primer.session.persistence import _CoalesceState, flush_partial_output, translate_stream_event

logger = logging.getLogger(__name__)

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

    Carries a coalescing state PER RUN (keyed by the run id, else the delegating call's id), so one subagent's text deltas accumulate independently of the
    parent turn's AND of every other run's, rather than interleaving into one another's buffers. Text only becomes a record at a ``Done`` or a tool call;
    a run that ends otherwise would lose what it had streamed, so a fatal ``Error`` flushes the run's buffers ahead of its own ERROR record (the
    translator does it), and :meth:`finish_run` does the same for a run that ended by an exception or a Stop (the invoke loops call it in a ``finally``). Before this the state was
    shared: a failed subagent's text surfaced later glued to the next run's, under the wrong run id (ticket 01a11ca9).
    """

    def __init__(
        self, *, writer: Any, event_bus: Any, session_id: str, turn_no: int = 0,
    ) -> None:
        self._writer = writer
        self._bus = event_bus
        self._session_id = session_id
        self._turn_no = turn_no
        self._states: dict[str, _CoalesceState] = {}

    @staticmethod
    def _run_key(delegate_tool_call_id: str | None, delegate_run_id: str | None) -> str:
        """A run is told apart by its run id; a caller that predates run ids has only the delegating call's (not unique) id."""
        return delegate_run_id or f"call:{delegate_tool_call_id}"

    def _stamp(self, rec: Any, **ids: Any) -> None:
        rec.payload["delegated"] = True
        rec.payload["delegate_tool_call_id"] = ids["delegate_tool_call_id"]
        for name in ("delegate_run_id", "delegate_parent_run_id", "delegate_depth"):
            if ids.get(name) is not None:
                rec.payload[name] = ids[name]

    async def _append(self, records: list[Any], **ids: Any) -> None:
        for rec in records:
            if _abandoned():
                return  # abandoned while an earlier record of this batch was being written
            self._stamp(rec, **ids)
            seq = await self._writer.append(rec)
            await self._bus.publish(
                f"session:{self._session_id}:tick", {"seq": seq},
            )

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
        key = self._run_key(delegate_tool_call_id, delegate_run_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _CoalesceState()
        records: list[Any] = []
        # A fatal Error ends this run's stream, and translate_stream_event itself flushes the run's buffers ahead of the ERROR record (the same
        # translator the main path uses), so what the run had streamed is written before the error that explains why it stops.
        result = translate_stream_event(ev, state, turn_no=self._turn_no)
        if result is not None:  # None: coalesced or not persistable; most events land here
            records.extend(result if isinstance(result, list) else [result])
        await self._append(
            records, delegate_tool_call_id=delegate_tool_call_id, delegate_run_id=delegate_run_id,
            delegate_parent_run_id=delegate_parent_run_id, delegate_depth=delegate_depth,
        )

    async def finish_run(
        self,
        *,
        delegate_tool_call_id: str | None,
        delegate_run_id: str | None = None,
        delegate_parent_run_id: str | None = None,
        delegate_depth: int | None = None,
        flush: bool = True,
    ) -> None:
        """A run is over, however it ended: write what it streamed and never got to flush, and forget its coalescing state.

        Called by the invoke loops in a ``finally``, so a run that raised, was stopped, or parked still leaves its text as its own record (a run that
        reached its ``Done`` has nothing buffered and writes nothing). A call that was abandoned by a Stop writes nothing (stop slice B1). Best effort: a
        failure to write must not replace the exception the run is already ending with.

        ``flush=False`` only forgets the run. The invoke loops pass it when the run ends by a CANCELLATION: a lost lease, a cancel the row does not flag, a
        force-deleted row all require the log to be left alone (the session may belong to another worker now), and when the cancel is one the turn does land,
        dispatch's cancelled exit decides what partial output is the turn's to write.
        """
        state = self._states.pop(self._run_key(delegate_tool_call_id, delegate_run_id), None)
        if state is None or not flush or _abandoned():
            return
        try:
            await self._append(
                flush_partial_output(state, turn_no=self._turn_no), delegate_tool_call_id=delegate_tool_call_id,
                delegate_run_id=delegate_run_id, delegate_parent_run_id=delegate_parent_run_id, delegate_depth=delegate_depth,
            )
        except Exception:  # noqa: BLE001 - best effort, see above
            logger.warning("delegation: could not write the unflushed output of run %s", delegate_run_id, exc_info=True)


__all__ = [
    "DelegationRecorder",
    "current_delegation_sink",
    "reset_delegation_sink",
    "set_delegation_sink",
]
