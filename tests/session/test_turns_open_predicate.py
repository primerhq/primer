"""Open-turn detection by terminal counting (S1 P1, plan Task 3).

Plan: docs/superpowers/plans/2026-08-16-s1-core-session-model.md
Spec: docs/superpowers/ux-revamp/02-s1-design.md section 4.
"""

import json

from primer.session.turns import count_turn_state, has_open_turn


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def test_closed_turn_is_not_open():
    lines = [_msg("user", "hi"), _rec(1, "user_input", text="hi"),
             _rec(2, "assistant_token", text="yo"), _rec(3, "done")]
    assert has_open_turn(lines, cursor=0) is False
    tc = count_turn_state(lines, cursor=0)
    assert (tc.open_user_inputs, tc.terminals, tc.max_seen_seq) == (1, 1, 3)


def test_unterminated_turn_is_open():
    lines = [_rec(1, "user_input", text="hi"), _rec(2, "assistant_token", text="...")]
    assert has_open_turn(lines, cursor=0) is True


def test_yielded_is_not_terminal():
    """A parked turn is still open; its resumed continuation closes it."""
    lines = [_rec(1, "user_input", text="hi"), _rec(2, "yielded", tool_name="ask_user")]
    assert has_open_turn(lines, cursor=0) is True


def test_cursor_bounds_the_scan():
    lines = [_rec(1, "user_input", text="a"), _rec(2, "done"),
             _rec(3, "user_input", text="b")]
    assert has_open_turn(lines, cursor=3) is True
    assert count_turn_state(lines, cursor=3).open_user_inputs == 1


def test_excluded_user_input_not_counted():
    lines = [_rec(1, "user_input", text="a", _history_excluded=True)]
    assert has_open_turn(lines, cursor=0) is False


def test_foreign_and_malformed_lines_are_skipped():
    """The log interleaves Message lines and may carry partial writes."""
    lines = ["", "not json at all", _msg("assistant", "hello"),
             json.dumps({"no_kind": True}), _rec(1, "user_input", text="a")]
    assert has_open_turn(lines, cursor=0) is True
