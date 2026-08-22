"""S7 section 6: a parked turn's wait segment.

The turn log supplies the segment when it exists (a yielded envelope
followed by its continuation); the record stream is the fallback, because
the default turn-log writer is the Noop writer, so many sessions have no
turns.jsonl at all.
"""

from __future__ import annotations

import json

from primer.session.timeline import build_turn_timeline


def _rec(seq, kind, ts, **payload):
    return json.dumps({
        "seq": seq, "kind": kind, "payload": payload, "created_at": ts,
    })


def _ev(seq, kind, turn_no, ts, **extra):
    return json.dumps({
        "seq": seq, "kind": kind, "turn_no": turn_no, "ts": ts, **extra,
    })


T0 = "2026-08-16T10:00:00+00:00"
T1 = "2026-08-16T10:00:01+00:00"
T5 = "2026-08-16T10:00:05+00:00"
T6 = "2026-08-16T10:00:06+00:00"


def _parked_records():
    return [
        _rec(1, "user_input", T0, text="hi"),
        _rec(2, "yielded", T1, event_key="ask_user:s:1", tool_name="ask_user",
             tool_call_id="c1"),
        _rec(3, "assistant_token", T5, text="thanks"),
        _rec(4, "done", T6, stop_reason="stop"),
    ]


def test_wait_comes_from_the_yielded_to_resumed_envelope_pair():
    log = [
        _ev(1, "started", 0, T0),
        _ev(2, "yielded", 0, T1, yield_kind="ask_user", event_key="ask_user:s:1"),
        _ev(3, "resumed", 1, T5, wait_ms=4000, resume_kind="event_fired"),
        _ev(4, "started", 1, T5),
        _ev(5, "completed", 1, T6, duration_ms=1000, finish_reason="stop"),
    ]
    tl = build_turn_timeline(
        message_lines=_parked_records(), turn_log_lines=log, turn_no=0,
    )
    assert len(tl["waits"]) == 1
    wait = tl["waits"][0]
    assert wait["from"] == T1
    assert wait["to"] == T5
    assert wait["ms"] == 4000
    assert wait["event_key"] == "ask_user:s:1"
    assert tl["status"] == "completed"
    assert tl["ended_at"] == T6


def test_wait_falls_back_to_the_records_when_there_is_no_turn_log():
    tl = build_turn_timeline(
        message_lines=_parked_records(), turn_log_lines=[], turn_no=0,
    )
    assert len(tl["waits"]) == 1
    assert tl["waits"][0]["ms"] == 4000
    assert tl["waits"][0]["event_key"] == "ask_user:s:1"


def test_an_unparked_turn_has_no_waits():
    lines = [
        _rec(1, "user_input", T0, text="hi"),
        _rec(2, "done", T1, stop_reason="stop"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["waits"] == []


def test_a_still_parked_turn_reports_no_wait_yet():
    lines = [
        _rec(1, "user_input", T0, text="hi"),
        _rec(2, "yielded", T1, event_key="ask_user:s:1", tool_name="ask_user"),
    ]
    tl = build_turn_timeline(message_lines=lines, turn_log_lines=[], turn_no=0)
    assert tl["waits"] == []
    assert tl["status"] == "running"
