"""S7 section 6 (crosscheck m4): seq windows by terminal counting.

A DONE whose stop_reason is "tool_use" closes a tool ROUND, not the turn:
the agent loop runs one llm.stream per round and every stream ends with
its own Done (primer/agent/loop.py, primer/llm/anthropic.py).
"""

from __future__ import annotations

import json

from primer.session.timeline import closes_turn, turn_windows


def _rec(seq, kind, **payload):
    return json.dumps({
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "created_at": "2026-08-16T00:00:00+00:00",
    })


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def test_tool_round_done_does_not_close_a_window():
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "tool_use"}}) is False
    assert closes_turn({"kind": "done", "payload": {"stop_reason": "stop"}}) is True
    assert closes_turn({"kind": "error", "payload": {}}) is True
    assert closes_turn({"kind": "cancelled", "payload": {}}) is True
    assert closes_turn({"kind": "yielded", "payload": {}}) is False


def test_multi_round_turn_is_one_window():
    lines = [
        _rec(1, "user_input", text="hi"),
        _rec(2, "tool_call", id="c1", name="bash"),
        _rec(3, "done", stop_reason="tool_use"),
        _rec(4, "tool_result", call_id="c1"),
        _rec(5, "assistant_token", text="ok"),
        _rec(6, "done", stop_reason="stop"),
    ]
    windows = turn_windows(lines)
    assert len(windows) == 1
    assert windows[0]["turn_no"] == 0
    assert windows[0]["terminal_seq"] == 6
    assert [r["seq"] for r in windows[0]["records"]] == [1, 2, 3, 4, 5, 6]


def test_queued_follow_up_starts_a_new_window():
    """A steer realized at the drain checkpoint opens the NEXT window."""
    lines = [
        _rec(1, "user_input", text="first"),
        _rec(2, "done", stop_reason="stop"),
        _rec(3, "user_input", text="queued follow-up"),
        _rec(4, "done", stop_reason="stop"),
    ]
    windows = turn_windows(lines)
    assert len(windows) == 2
    assert [w["turn_no"] for w in windows] == [0, 1]
    assert [r["seq"] for r in windows[0]["records"]] == [1, 2]
    assert [r["seq"] for r in windows[1]["records"]] == [3, 4]


def test_parked_turn_stays_open_until_its_continuation_closes_it():
    lines = [
        _rec(1, "user_input", text="hi"),
        _rec(2, "yielded", event_key="ask_user:s:1"),
        _rec(3, "assistant_token", text="resumed"),
        _rec(4, "done", stop_reason="stop"),
    ]
    windows = turn_windows(lines)
    assert len(windows) == 1
    assert [r["seq"] for r in windows[0]["records"]] == [1, 2, 3, 4]


def test_unterminated_tail_is_its_own_window():
    lines = [
        _rec(1, "user_input", text="a"),
        _rec(2, "done", stop_reason="stop"),
        _rec(3, "user_input", text="b"),
    ]
    windows = turn_windows(lines)
    assert len(windows) == 2
    assert windows[1]["terminal_seq"] is None


def test_message_lines_and_junk_are_skipped():
    lines = ["", "not json", _msg("user", "hi"), _rec(1, "done", stop_reason="stop")]
    windows = turn_windows(lines)
    assert len(windows) == 1
    assert [r["seq"] for r in windows[0]["records"]] == [1]


def test_rewound_rows_are_invisible_but_the_turn_keeps_its_ordinal():
    """Folding hides CONTENT; it must never renumber turns.

    The turn log is never folded, so if a rewind collapsed two windows
    into one, turn 1's tree would be served from turn 0's envelope.
    """
    lines = [
        _rec(1, "user_input", text="a"),
        _rec(2, "done", stop_reason="stop"),
        _rec(3, "user_input", text="b"),
        _rec(4, "rewind_marker", to_seq=2),
    ]
    windows = turn_windows(lines)
    assert len(windows) == 2
    assert [r["seq"] for r in windows[0]["records"]] == [1, 2]
    assert windows[1]["turn_no"] == 1
    assert windows[1]["records"] == []


def test_compaction_does_not_renumber_the_turns_after_it():
    """A compaction marker replaces every visible row before it.

    Counting turns over visible_records would drop window 0 entirely and
    slide the second turn into index 0, silently retargeting every
    /turns/{turn_no}/timeline URL already in circulation.
    """
    lines = [
        _rec(1, "user_input", text="a"),
        _rec(2, "done", stop_reason="stop"),
        _rec(3, "compaction_marker", summary="earlier turns, folded"),
        _rec(4, "user_input", text="b"),
        _rec(5, "done", stop_reason="stop"),
    ]
    windows = turn_windows(lines)
    assert [w["turn_no"] for w in windows] == [0, 1]
    assert windows[0]["records"] == []
    assert [r["seq"] for r in windows[1]["records"]] == [3, 4, 5]
