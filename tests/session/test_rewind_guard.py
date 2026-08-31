"""Rewind target guards (S1 P2 Task 11; spec section 5, amendment C2)."""

import json

import pytest

from primer.model.except_ import ConflictError, ValidationError
from primer.session.rewind import check_rewind_target


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


def _log():
    return [
        _rec(1, "user_input", text="first"),
        _rec(2, "assistant_token", text="ok"),
        _rec(3, "done"),
        _rec(4, "user_input", text="second"),
        _rec(5, "assistant_token", text="also ok"),
        _rec(6, "done"),
    ]


def test_valid_user_input_target_passes():
    check_rewind_target(_log(), to_seq=1)


def test_non_user_input_target_is_rejected():
    with pytest.raises(ValidationError):
        check_rewind_target(_log(), to_seq=3)


def test_unknown_seq_is_rejected():
    with pytest.raises(ValidationError):
        check_rewind_target(_log(), to_seq=99)


def test_newest_visible_record_leaves_nothing_to_discard():
    lines = _log() + [_rec(7, "user_input", text="third")]
    with pytest.raises(ValidationError):
        check_rewind_target(lines, to_seq=7)


def test_rewind_into_a_compacted_span_is_a_conflict():
    """Amendment C2: the folded rows are gone and the summary would
    drop too, so the turn would rebuild from an empty prompt."""
    lines = _log() + [
        _rec(7, "compaction_marker", summary="so far", replaced_to_seq=6),
        _rec(8, "user_input", text="after the fold"),
        _rec(9, "done"),
    ]
    with pytest.raises(ConflictError):
        check_rewind_target(lines, to_seq=4)
    check_rewind_target(lines, to_seq=8)  # after the marker is fine


def test_target_already_rewound_away_is_rejected():
    lines = _log() + [_rec(7, "rewind_marker", to_seq=3, actor="user")]
    with pytest.raises(ValidationError):
        check_rewind_target(lines, to_seq=4)


def test_empty_log_is_rejected():
    with pytest.raises(ValidationError):
        check_rewind_target([], to_seq=1)
