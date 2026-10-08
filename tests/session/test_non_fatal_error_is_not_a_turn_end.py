"""A NON-fatal stream Error is not the end of a turn (ticket 01a11bf6).

``chat.Error.fatal`` is documented as "True if no further events will follow this one": ``fatal=False`` is a recoverable error the stream
reports and then goes on from (the OpenResponses client emits one for an ``error`` event). The persistence layer writes every Error as an
ERROR record with ``payload.fatal``, and every server reader decided "this record ends a turn" through ``primer.session.terminals``, which counted
EVERY ERROR record: the timeline split the turn into two windows (and shifted the trace ordinal of every turn after it), ``session_usage``
counted an extra turn, the open-turn count read the turn as closed, the final-result relay took the error as the boundary and relayed only
the text after it, and a log ending in the error read as a failed turn.

The rule now: an ERROR record with an EXPLICIT ``fatal: false`` is not a session terminal. A record with no ``fatal`` (older records, the
graph runtime's errors, the dispatch failure exit's ERROR), ``fatal: true`` and ``fatal: null`` stay terminals: only an explicit false is
a notice. All the readers share ``is_session_terminal`` / ``closes_turn``, so one rule moves them all; the console's mirror
(``SH_closesTurn``) is pinned against it in tests/ui/test_shell_turns.py.
"""

from __future__ import annotations

import json

import pytest

from primer.channel.session_relay import derive_session_final_text
from primer.session.terminals import closes_turn, is_session_terminal
from primer.session.timeline import _turn_status, turn_windows
from primer.session.turns import count_turn_state, has_open_turn
from primer.session.usage import session_usage

NON_FATAL = {"message": "the model reported a problem", "code": "server_error", "fatal": False}


def _rec(seq: int, kind: str, **payload) -> dict:
    return {"seq": seq, "kind": kind, "payload": payload, "created_at": "2026-10-08T00:00:00+00:00"}


def _lines(records: list[dict]) -> list[str]:
    return [json.dumps(r) for r in records]


# user turn 1 has a non-fatal Error in the middle of its stream and then finishes; turn 2 is plain
RECOVERED = [
    _rec(1, "user_input", text="turn 1"),
    _rec(2, "assistant_token", text="Hello"),
    _rec(3, "error", **NON_FATAL),
    _rec(4, "assistant_token", text=" world"),
    _rec(5, "done", stop_reason="stop"),
    _rec(6, "user_input", text="turn 2"),
    _rec(7, "assistant_token", text="Second"),
    _rec(8, "done", stop_reason="stop"),
]


@pytest.mark.parametrize(
    ("payload", "ends_the_turn"),
    [
        ({"fatal": False}, False),
        ({"fatal": True}, True),
        ({"fatal": None}, True),                  # only an EXPLICIT false is a notice
        ({}, True),                                # records from before the flag, the graph runtime's errors, the failure exit's ERROR
        ({"message": "boom", "code": "x"}, True),
        ({"fatal": False, "delegated": True}, False),
        ({"fatal": True, "delegated": True}, False),     # a subagent's terminal is never the session's, as before
    ],
)
def test_only_an_error_that_says_it_is_not_fatal_is_not_a_turn_end(payload, ends_the_turn) -> None:
    rec = {"kind": "error", "payload": payload}

    assert closes_turn(rec) is ends_the_turn
    assert is_session_terminal(rec) is ends_the_turn


def test_the_other_terminal_kinds_are_untouched() -> None:
    assert closes_turn({"kind": "cancelled", "payload": {"fatal": False}}) is True
    assert closes_turn({"kind": "done", "payload": {"fatal": False, "stop_reason": "stop"}}) is True
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "tool_use"}}) is False


def test_a_non_fatal_error_does_not_split_the_turn_into_two_windows() -> None:
    windows = turn_windows(_lines(RECOVERED))

    assert [w["turn_no"] for w in windows] == [0, 1], f"the non-fatal error opened a window: {[w['turn_no'] for w in windows]}"
    assert [w["terminal_seq"] for w in windows] == [5, 8], "each turn ends at its own done, so the second turn keeps ordinal 1"
    assert [r["seq"] for r in windows[0]["records"]] == [1, 2, 3, 4, 5], "the error stays inside the turn it happened in"


def test_session_usage_counts_the_turn_once() -> None:
    assert session_usage(_lines(RECOVERED)).turns == 2


def test_a_turn_whose_stream_reported_a_non_fatal_error_is_still_open() -> None:
    log = _lines(RECOVERED[:3])          # user_input, some text, then the non-fatal error: nothing has ended the turn

    assert has_open_turn(log, cursor=1) is True
    assert count_turn_state(log, cursor=1).terminals == 0
    failed = _lines([*RECOVERED[:2], _rec(3, "error", message="boom", code="x", fatal=True)])
    assert has_open_turn(failed, cursor=1) is False, "a fatal error still closes the turn"


def test_the_timeline_does_not_call_a_turn_failed_because_its_last_record_is_a_non_fatal_error() -> None:
    assert _turn_status([], RECOVERED[:3]) == "running"
    assert _turn_status([], [*RECOVERED[:2], _rec(3, "error", message="boom", fatal=True)]) == "failed"
    assert _turn_status([], [*RECOVERED[:2], _rec(3, "error", message="boom")]) == "failed", "no flag: a failure, as before"


def test_the_final_text_relay_joins_the_whole_turn_across_a_non_fatal_error() -> None:
    text = derive_session_final_text(RECOVERED[:5])

    assert text is not None and "Hello" in text and "world" in text, f"the text before the non-fatal error was cut off: {text!r}"
