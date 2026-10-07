"""``turns`` is the number of turns in view; ``model_calls`` is the number of model calls (ticket 01a1138d).

``session_usage`` used to do ``turns += 1`` for EVERY visible ``done``, so a one-tool-round turn (``done(tool_use)`` then
``done(stop)``) reported ``turns == 2``, and a subagent's own dones counted too. That number is the spend ledger's
denominator (every model call, the same population as ``total_*`` and ``last_*``): it is now ``model_calls``.
``turns`` is corrected to the SHARED turn rule (``primer.session.terminals.closes_turn``), so it equals the trace ordinals:
a session terminal (``done`` that is not a tool round, ``error``, ``cancelled``) that is not a subagent's.

Deliberately MIRRORED, quirk included (the lead's ruling of 2026-10-07: no second rule): a ``done`` followed by the
``cancelled`` of a Stop that landed after the model finished is two terminals, so that one user turn counts twice, exactly as
``timeline.turn_windows`` splits it into two windows (ticket 01a115ee-7384). Fixing the shared predicate fixes this too.

Scope is the VISIBLE set, as before: rewound turns stop counting and a compaction folds the earlier ones away, so ``turns`` is
the turns IN VIEW, not the session's lifetime (that is the row's ``turn_no``).
"""

from __future__ import annotations

import json

from primer.session.timeline import turn_windows
from primer.session.usage import session_usage


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": "2026-10-07T00:00:00+00:00"})


def _usage(i, o):
    return {"input_tokens": i, "output_tokens": o}


# One user turn of three model rounds with a subagent run inside it, then a second turn that a Stop ended with no done.
MIXED = [
    _rec(1, "user_input", text="turn 1"),
    _rec(2, "done", stop_reason="tool_use", usage=_usage(100, 10)),
    _rec(3, "done", stop_reason="tool_use", usage=_usage(200, 10), delegated=True, delegate_run_id="r1"),
    _rec(4, "done", stop_reason="stop", usage=_usage(210, 10), delegated=True, delegate_run_id="r1"),
    _rec(5, "done", stop_reason="tool_use", usage=_usage(300, 10)),
    _rec(6, "done", stop_reason="stop", usage=_usage(400, 20)),
    _rec(7, "user_input", text="turn 2"),
    _rec(8, "assistant_token", text="partial"),
    _rec(9, "cancelled", reason="operator_interrupt"),
]


def test_turns_counts_the_turns_and_model_calls_counts_every_done():
    u = session_usage(MIXED)

    assert u.turns == 2, "turn 1 (ended by its stop done, seq 6) and turn 2 (ended by the Stop, seq 9)"
    assert u.model_calls == 5, "every visible done: the three rounds and the subagent's two"


def test_the_token_fields_are_still_one_population_with_model_calls():
    u = session_usage(MIXED)

    assert (u.total_input_tokens, u.total_output_tokens) == (100 + 200 + 210 + 300 + 400, 10 + 10 + 10 + 10 + 20)
    assert (u.last_input_tokens, u.last_output_tokens) == (400, 20), "the newest done that had an envelope"


def test_turns_equals_the_trace_ordinals():
    """The trace numbers turns by ``turn_windows``: the windows that have a terminal are the turns."""
    assert session_usage(MIXED).turns == sum(1 for w in turn_windows(MIXED) if w["terminal_seq"] is not None)


def test_a_one_tool_round_turn_is_one_turn_and_two_model_calls():
    u = session_usage([
        _rec(1, "user_input", text="go"),
        _rec(2, "done", stop_reason="tool_use", usage=_usage(10, 1)),
        _rec(3, "done", stop_reason="stop", usage=_usage(20, 2)),
    ])

    assert (u.turns, u.model_calls) == (1, 2)


def test_a_subagents_dones_are_model_calls_and_never_turns():
    u = session_usage([
        _rec(1, "user_input", text="go"),
        _rec(2, "done", stop_reason="stop", usage=_usage(5, 1), delegated=True, delegate_run_id="r1"),
        _rec(3, "done", stop_reason="stop", usage=_usage(20, 2)),
    ])

    assert (u.turns, u.model_calls) == (1, 2)
    assert u.total_input_tokens == 25, "a delegated call's tokens are the session's spend"


def test_a_turn_that_has_not_ended_is_not_a_turn_yet():
    u = session_usage([
        _rec(1, "user_input", text="go"),
        _rec(2, "done", stop_reason="tool_use", usage=_usage(10, 1)),
    ])

    assert (u.turns, u.model_calls) == (0, 1)


def test_a_turn_that_ended_in_an_error_with_no_done_is_a_turn():
    u = session_usage([_rec(1, "user_input", text="go"), _rec(2, "error", code="llm_connect_error", message="down")])

    assert (u.turns, u.model_calls) == (1, 0)


def test_a_done_then_the_cancelled_of_a_late_stop_counts_twice_like_the_trace_does():
    """MIRRORS ``closes_turn`` on purpose (ticket 01a115ee-7384 tracks the shared predicate): ``turn_windows`` makes the same
    two windows out of this one user turn, so the two numbers stay equal."""
    log = [
        _rec(1, "user_input", text="hi"),
        _rec(2, "assistant_token", text="the answer"),
        _rec(3, "done", stop_reason="stop", usage=_usage(10, 5)),
        _rec(4, "cancelled", reason="operator_interrupt"),
    ]

    u = session_usage(log)

    assert u.turns == 2 and u.model_calls == 1
    assert u.turns == sum(1 for w in turn_windows(log) if w["terminal_seq"] is not None)


def test_rewound_turns_stop_counting_both_numbers():
    u = session_usage([
        _rec(1, "user_input", text="a"),
        _rec(2, "done", stop_reason="tool_use", usage=_usage(10, 1)),
        _rec(3, "done", stop_reason="stop", usage=_usage(20, 2)),
        _rec(4, "user_input", text="b"),
        _rec(5, "done", stop_reason="stop", usage=_usage(30, 3)),
        _rec(6, "rewind_marker", to_seq=3),
    ])

    assert (u.turns, u.model_calls) == (1, 2)


def test_after_a_compaction_turns_counts_the_turns_in_view():
    u = session_usage([
        _rec(1, "user_input", text="a"),
        _rec(2, "done", stop_reason="stop", usage=_usage(10, 1)),
        _rec(3, "user_input", text="b"),
        _rec(4, "done", stop_reason="stop", usage=_usage(20, 2)),
        _rec(5, "compaction_marker", summary="s", replaced_to_seq=4),
        _rec(6, "user_input", text="c"),
        _rec(7, "done", stop_reason="tool_use", usage=_usage(5, 1)),
        _rec(8, "done", stop_reason="stop", usage=_usage(6, 1)),
    ])

    assert (u.turns, u.model_calls) == (1, 2), "the two folded turns are not in view"
