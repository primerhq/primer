"""Read-time replay: compaction and rewind folding (S1 P2).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 5.

The log is append-only and nothing is ever deleted, so what a reader
sees is computed by walking it in order and maintaining a visible set:
a compaction_marker folds everything currently visible before it into
its summary, and a rewind_marker drops visible rows past its to_seq.
The rules compose, which is the point: rewind, continue, rewind again
nests correctly, and compactions inside a rewound span stay hidden.
"""

import json

from primer.session.replay import visible_records


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


def _seqs(lines):
    return [r["seq"] for r in visible_records(lines)]


def test_plain_log_is_fully_visible():
    lines = [_rec(1, "user_input"), _rec(2, "assistant_token"), _rec(3, "done")]
    assert _seqs(lines) == [1, 2, 3]


def test_rewind_drops_rows_past_the_target():
    lines = [_rec(1, "user_input"), _rec(2, "assistant_token"), _rec(3, "done"),
             _rec(4, "rewind_marker", to_seq=1)]
    assert _seqs(lines) == [1]


def test_rows_after_a_rewind_stay_visible():
    """The marker drops what preceded it, never what follows."""
    lines = [_rec(1, "user_input"), _rec(2, "done"),
             _rec(3, "rewind_marker", to_seq=1),
             _rec(4, "user_input"), _rec(5, "done")]
    assert _seqs(lines) == [1, 4, 5]


def test_repeated_rewinds_nest():
    lines = [_rec(1, "user_input"), _rec(2, "done"),
             _rec(3, "rewind_marker", to_seq=1),
             _rec(4, "user_input"), _rec(5, "done"),
             _rec(6, "rewind_marker", to_seq=4)]
    assert _seqs(lines) == [1, 4]


def test_compaction_folds_everything_visible_before_it():
    lines = [_rec(1, "user_input"), _rec(2, "assistant_token"),
             _rec(3, "compaction_marker", summary="so far"),
             _rec(4, "user_input")]
    assert _seqs(lines) == [3, 4]


def test_rewind_into_a_compacted_span_leaves_nothing_visible():
    """Why the API must refuse such a rewind (crosscheck amendment C2).

    Seq 1 was folded into the compaction at seq 2, so it is no longer in
    the visible set and a rewind to it cannot restore it: the marker
    itself has seq 2 > 1 and is dropped too. The walk is correct; the
    result is an empty prompt, which is exactly the hole C2 closes by
    rejecting a rewind whose target predates the latest visible
    compaction, rather than by special-casing it here.
    """
    lines = [_rec(1, "user_input"),
             _rec(2, "compaction_marker", summary="s"),
             _rec(3, "user_input"),
             _rec(4, "rewind_marker", to_seq=1)]
    assert _seqs(lines) == []


def test_rewind_after_a_compaction_keeps_the_summary():
    """A legal rewind, landing after the compaction, keeps its summary."""
    lines = [_rec(1, "user_input"),
             _rec(2, "compaction_marker", summary="s"),
             _rec(3, "user_input"),
             _rec(4, "done"),
             _rec(5, "rewind_marker", to_seq=3)]
    assert _seqs(lines) == [2, 3]


def test_compaction_after_a_rewind_folds_only_what_survived():
    lines = [_rec(1, "user_input"), _rec(2, "done"),
             _rec(3, "rewind_marker", to_seq=1),
             _rec(4, "user_input"),
             _rec(5, "compaction_marker", summary="s")]
    assert _seqs(lines) == [5]


def test_markers_are_never_returned_as_content_kinds():
    lines = [_rec(1, "user_input"), _rec(2, "rewind_marker", to_seq=1)]
    kinds = [r["kind"] for r in visible_records(lines)]
    assert "rewind_marker" not in kinds


def test_malformed_and_foreign_lines_are_skipped():
    lines = ["", "not json", json.dumps({"role": "user", "parts": []}),
             _rec(1, "user_input")]
    assert _seqs(lines) == [1]
