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

A NON-fatal ``error`` is neither (ticket 01a11bf6). ``chat.Error.fatal`` is "True if no further events will follow this one", so an
Error written with ``payload.fatal`` explicitly ``false`` is a recoverable error the stream reported and went on from: it stays inside
the turn it happened in. Only an EXPLICIT false counts: an ``error`` with no ``fatal`` (records from before the flag, the graph runtime's
errors, the dispatch failure exit's own ERROR record), ``fatal: true`` and ``fatal: null`` are terminals, as they always were. The console's
mirror, ``SH_closesTurn`` (ui/foundation/shell-turns.js), says the same, and tests/ui/test_shell_turns.py compares the two over every
variant.

One FAILED turn is one window (ticket 01a11ca5). A failed model call writes the stream's own terminal, then dispatch's failure-exit ERROR with
the same words, then the claim adapter's release marker, and ``closes_turn`` is per record, so each of them used to end a window of its own:
two or three turns for one failure, in the timeline's windows, the trace's ordinals, ``session_usage.turns`` and the open-turn count. Whether a
record is a COPY of an earlier failure needs the records before it, so the windowing readers walk the log through :class:`TurnWindowScanner`:
the first error of a failure ends the window and the rest of the failure is filed with it. Everything that asks only "is this record a
terminal of the session's own run" (a turn's status, the final text, the relay's boundaries) keeps using the per-record predicates, so a copy at
the END of the log still reads as a terminal. Folded on read, so logs written before the rule fold the same way. The console's mirror is
``SH_newWindowScanner`` / ``SH_windowsOfSeq`` (ui/foundation/shell-turns.js); tests/ui/test_shell_turns.py compares the two over the shapes the writers produce.

A GRAPH turn is one window too (ticket 01a11f35). Every node of a graph writes its own ``done`` (or ``error`` / ``cancelled``) and each such record carries the node's ``node_id``. A record with a
``node_id`` is INSIDE the window, exactly as a delegated record is: it ends nothing, is a copy of nothing and changes nothing in the scanner but its graph-open flag (rule (c) below), so one fan-out of
two workers is one window instead of three. What closes it is the graph's OWN END, a node-less ``done`` that the writers append when the run ends (:func:`is_graph_end`, ``payload.graph_end``): session dispatch's clean
completion for a graph run (``stop_reason`` ``stop`` when the graph ended ``completed``, ``error`` when it did not, so every reader of a failed turn reads it as one), and the graph resume
coordinators (``resume_graph_engine`` and ``resume_graph_tool_wait``, through ``end_graph``) before they end a resumed graph. NOT EVERY PATH THAT ENDS A GRAPH SESSION WRITES IT: a parked graph that is
cancelled is ended inline by ``cancel_session`` with no record (agents have the same gap: a ticket), and so is a resumable row whose cancel the pool finds when it claims it and the preempt-cancel
convergence (both in the pool's resume branch); ``resume_engine_session`` ends a graph itself on three early exits and ``resume_engine_tool_wait`` on two (a malformed ``parked_state``, no storage); a
log written before the end was a record has none. The executor's own stream writes none (it ends with the End node's output and the end node's exit transition), and the claim adapter's release marker
that may follow a failed end is a copy of it. A graph that parks is not over, so nothing closes it until its resume ends it. The per-record predicates are unchanged on purpose: the final-result
relay reads the text before the last node's ``done`` and the End output after it (and treats the graph's end as the verdict, not as a text boundary). A graph log written before records carried a
``node_id`` cannot be told from a non-graph log and keeps the windows it had. For every log with a graph run that ended without its end record, rule (c): an ``invocation_divider`` is written ONLY to an ENDED
session, so a graph run still open before one (a node's record since the last close) ended without its end, and the divider closes that window (and restarts the failure fold: a graph restarted with no
message writes a divider and no ``user_input``). The residual, accepted and documented: the LAST invocation of a session that ended with no end record stays one open window until a reopen
(``usage.turns`` one short, ``terminal_seq`` None; its status comes from ``turns.jsonl``).
Folded on read like the rest, so old graph sessions renumber on the next read (the trace asks for the ordinal the console counted over the same records, so the two stay in step).
"""

from __future__ import annotations

from typing import Any

from primer.model.workspace_session import SessionMessageKind

_DONE = SessionMessageKind.DONE.value
_ERROR = SessionMessageKind.ERROR.value
_CANCELLED = SessionMessageKind.CANCELLED.value
_USER_INPUT = SessionMessageKind.USER_INPUT.value
_INVOCATION_DIVIDER = SessionMessageKind.INVOCATION_DIVIDER.value

TERMINAL_KINDS = frozenset({_DONE, _ERROR, _CANCELLED})


def payload_of(rec: dict[str, Any]) -> dict[str, Any]:
    """``rec``'s payload as a dict: a log is written by many versions, and a payload that is not an object (a string, a list, a number, ``null``) reads as
    an empty one, here and in the console's mirror, instead of crashing the reader."""
    payload = rec.get("payload")
    return payload if isinstance(payload, dict) else {}


def is_delegated(rec: dict[str, Any]) -> bool:
    """True when ``rec`` was written by a delegated (subagent) run rather than by the session's own turn."""
    return bool(payload_of(rec).get("delegated"))


def is_non_fatal_error(rec: dict[str, Any]) -> bool:
    """True for an ``error`` record that says, with an explicit ``payload.fatal`` of ``false``, that the stream went on from it."""
    return rec.get("kind") == _ERROR and payload_of(rec).get("fatal") is False


def is_session_terminal(rec: dict[str, Any]) -> bool:
    """True when ``rec`` is a ``done`` / ``error`` / ``cancelled`` of the session's OWN run.

    A delegated run's terminal is the subagent's turn end, never the session's, so it is not one; neither is a non-fatal ``error``
    (a recoverable stream error is a notice inside the turn, see the module docstring).
    """
    return rec.get("kind") in TERMINAL_KINDS and not is_delegated(rec) and not is_non_fatal_error(rec)


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
        return payload_of(rec).get("stop_reason") != "tool_use"
    return True


def is_bare_marker(rec: dict[str, Any]) -> bool:
    """True for the claim adapter's release marker: an ``error`` that is ``terminal`` and says nothing (no message, code or title)."""
    payload = payload_of(rec)
    return rec.get("kind") == _ERROR and payload.get("terminal") is True and not payload.get("message") and not payload.get("code") and not payload.get("title")


def is_dispatch_failure_record(rec: dict[str, Any]) -> bool:
    """True for the ERROR record ``dispatch._end_turn_failed`` writes: built from the problem details, so it has a ``title`` and an integer ``status`` and no ``fatal`` flag
    (a stream's own error has a ``fatal``; a graph node's has neither title nor status). It names no node, which the scanner does not ask here: a record with a node is inside its window before it
    is asked.

    It is written exactly once, by the failure exit, AFTER the failure it describes, so it is a copy of the open turn's failure whatever its words
    are: its message is the problem detail, and records written before ticket 01a11f35-ad20 stored the stream's message raw (it is redacted at write
    time now, but old logs are not migrated), so comparing the words would take a copy for a new failure.
    """
    payload = payload_of(rec)
    status = payload.get("status")
    return rec.get("kind") == _ERROR and isinstance(payload.get("title"), str) and isinstance(status, int) and not isinstance(status, bool) and "fatal" not in payload


def is_graph_end(rec: dict[str, Any]) -> bool:
    """True for the record that ends a graph RUN: the node-less ``done`` the writers append after the last node (``payload.graph_end``; see the module docstring).

    ``stop_reason`` is ``stop`` when the graph ended ``completed`` and ``error`` when it did not. It carries no usage, and it is not a model call.
    """
    return rec.get("kind") == _DONE and payload_of(rec).get("graph_end") is True and not rec.get("node_id") and not is_delegated(rec)


# What :meth:`TurnWindowScanner.feed` says about a record.
CLOSES = "closes"        # the record ends a window
COPY = "copy"            # a copy of a failure that already ended one: ends nothing, and belongs to the window it copies
INSIDE = "inside"        # an ordinary record of the open window


class TurnWindowScanner:
    """Feed the records of a log in order; it says which ones end a window, which are copies of a failure that did, and which are neither.

    The rule, per record (a delegated record is always ``INSIDE``: a subagent's terminal and a subagent's failure are never the session's; so is a graph NODE's record, one that
    carries a ``node_id``: the graph turn is not over until the graph's own end, the first ``done`` / ``error`` / ``cancelled`` that names no node; such a record only marks a graph run open for
    rule (c) below; a ``user_input`` or a ``yielded`` names no node either and ends nothing, and an ``invocation_divider`` ends a window only in the one case below):

    * ``user_input`` starts a new turn: nothing before it can be copied from.
    * ``done`` / ``cancelled`` (a ``done`` that is not a tool round's) ends a window. A ``done`` with ``stop_reason: "error"`` (OpenResponses
      ``response.failed``) is a FAILURE end, and what follows it in the turn can copy it; any other end closes the turn, so the next error is new.
    * an ``error`` that says ``fatal: false`` is a notice: it ends nothing, and its words are remembered, because for OpenResponses it is the
      cause that arrives AFTER the ``done(error)`` it belongs to (the agent loop holds the first Done / Error of a stream and yields it last).
    * a bare release marker is a ``COPY`` once the turn has failed, and the only evidence (so it ends the window) when nothing has.
    * dispatch's own failure ERROR (:func:`is_dispatch_failure_record`) is a ``COPY`` once the turn has failed, whatever its words.
    * a graph's own end (:func:`is_graph_end`) is a ``COPY`` once the turn has failed (a graph-level error such as ``max_iterations_exceeded`` names no node, so it has already ended the
      window), and an ordinary terminal otherwise.
    * an ``invocation_divider`` is written only to an ENDED session, so a graph run still open before it (a node's record since the last close) ended without its end record: the divider CLOSES
      that window (it is its last record). Otherwise it is ``INSIDE``, the first record of the next window. Either way it starts a new turn: the failure state of the invocation before it
      is forgotten, because nothing before a reopen is a copy of anything after it.
    * any other ``error`` ends a window, unless the turn has already failed and an earlier error of the turn has the same non-empty message.

    A notice alone does not make a turn failed: with no ``response.failed`` the dispatch error that follows it (same words) is the only end the
    turn has, so it ends the window instead of being filed as a copy of a notice.
    """

    def __init__(self) -> None:
        self._failed = False
        self._words: list[str] = []
        self._graph_open = False          # a graph node's record since the last close: a graph run no terminal has closed yet

    def _new_turn(self) -> None:
        self._failed = False
        self._words = []

    def _remember(self, message: str | None) -> None:
        if message:
            self._words.append(message)

    def _copies_an_earlier_error(self, message: str | None) -> bool:
        return bool(message) and message in self._words

    def feed(self, rec: dict[str, Any]) -> str:
        verdict = self._feed(rec)
        # A close ends the graph run, and so does a COPY: the graph's end after a failure that already ended the window is the end of the run it follows (a late node record between the two opened it
        # again), so a divider after it must not read the run as open (round 3 review: no dependence on the order of those records).
        if verdict in (CLOSES, COPY):
            self._graph_open = False
        return verdict

    def _feed(self, rec: dict[str, Any]) -> str:
        if rec.get("node_id"):
            self._graph_open = True
        if is_delegated(rec) or rec.get("node_id"):          # a subagent's record, or a graph node's: the turn is the session's / the graph's, and it is not over
            return INSIDE
        kind = rec.get("kind")
        if kind == _INVOCATION_DIVIDER:
            # Written only to an ENDED session (reset._reopen_ended_locked, wake_session's ENDED branch, restart): the invocation before it is over. A graph run it finds open ended WITHOUT its end
            # record (a log from before the end was a record, a parked graph that was cancelled, a resume path that writes none), so the divider closes it; and nothing written before a divider is a
            # copy of anything after it (a graph restarted with no message writes a divider and no user_input, so the failure fold restarts here). After a closed turn it stays inside, the
            # first record of the next window.
            closes = self._graph_open
            self._new_turn()
            return CLOSES if closes else INSIDE
        if kind == _USER_INPUT:
            self._new_turn()
            return INSIDE
        if kind == _ERROR:
            payload = payload_of(rec)
            message = payload.get("message") if isinstance(payload.get("message"), str) else None
            if is_bare_marker(rec):
                if self._failed:
                    return COPY
                self._failed = True
                return CLOSES
            if is_non_fatal_error(rec):
                self._remember(message)
                return INSIDE
            if self._failed and (is_dispatch_failure_record(rec) or self._copies_an_earlier_error(message)):
                return COPY
            self._remember(message)
            self._failed = True
            return CLOSES
        if self._failed and is_graph_end(rec):
            return COPY          # the graph's end after a failure that already ended the window (a graph-level error, which names no node): a copy of it
        if not closes_turn(rec):
            return INSIDE
        if kind == _DONE and payload_of(rec).get("stop_reason") == "error":
            self._failed = True
        else:
            self._new_turn()
        return CLOSES


__all__ = [
    "CLOSES", "COPY", "INSIDE", "TERMINAL_KINDS", "TurnWindowScanner", "closes_turn", "payload_of", "is_bare_marker", "is_delegated", "is_dispatch_failure_record",
    "is_graph_end", "is_non_fatal_error", "is_session_terminal",
]
