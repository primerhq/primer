"""S7 section 6: turn-log envelopes joined to a window by turn_no.

A park does not bump turn_no (primer/claim/adapters/sessions.py) but the
resume injection release does, so one logical turn can span the yielding
envelope AND its continuation envelope.
"""

from __future__ import annotations

import json

from primer.session.timeline import envelopes_for_window, turn_envelopes


def _ev(seq, kind, turn_no, **extra):
    return json.dumps({
        "seq": seq,
        "kind": kind,
        "ts": "2026-08-16T00:00:00+00:00",
        "turn_no": turn_no,
        **extra,
    })


def test_events_group_by_turn_no_in_ascending_order():
    lines = [
        _ev(3, "started", 1),
        _ev(1, "started", 0),
        _ev(2, "completed", 0, duration_ms=10),
        _ev(4, "completed", 1, duration_ms=20),
    ]
    groups = turn_envelopes(lines)
    assert [g[0]["turn_no"] for g in groups] == [0, 1]
    assert [e["seq"] for e in groups[0]] == [1, 2]


def test_events_without_a_turn_no_are_skipped():
    lines = [_ev(1, "started", 0), json.dumps({"seq": 2, "kind": "started"})]
    assert len(turn_envelopes(lines)) == 1


def test_bogus_lines_are_skipped():
    lines = ["", "not json", json.dumps([1, 2]), _ev(1, "started", 0)]
    assert len(turn_envelopes(lines)) == 1


def test_plain_turn_maps_one_to_one():
    groups = turn_envelopes([
        _ev(1, "started", 0), _ev(2, "completed", 0),
        _ev(3, "started", 1), _ev(4, "completed", 1),
    ])
    assert [e["turn_no"] for e in envelopes_for_window(groups, 0)[0]] == [0, 0]
    assert [e["turn_no"] for e in envelopes_for_window(groups, 1)[0]] == [1, 1]


def test_parked_turn_absorbs_its_continuation_envelope():
    groups = turn_envelopes([
        _ev(1, "started", 0),
        _ev(2, "yielded", 0, yield_kind="ask_user", event_key="ask_user:s:1"),
        _ev(3, "started", 1),
        _ev(4, "completed", 1, duration_ms=90),
        _ev(5, "started", 2),
        _ev(6, "completed", 2, duration_ms=5),
    ])
    window0 = envelopes_for_window(groups, 0)
    assert len(window0) == 2
    assert [e["turn_no"] for e in window0[0]] == [0, 0]
    assert [e["turn_no"] for e in window0[1]] == [1, 1]
    window1 = envelopes_for_window(groups, 1)
    assert len(window1) == 1
    assert window1[0][0]["turn_no"] == 2


def test_out_of_range_index_is_empty():
    groups = turn_envelopes([_ev(1, "started", 0), _ev(2, "completed", 0)])
    assert envelopes_for_window(groups, 5) == []
    assert envelopes_for_window(groups, -1) == []
    assert envelopes_for_window([], 0) == []
