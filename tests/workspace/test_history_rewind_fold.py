"""Rewind folding in the shared history reader (S1 P2).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 5.

messages.jsonl interleaves seqless role/parts Message lines (the LLM
history) with seq-bearing event records. A rewind marker names a seq,
so folding Messages is necessarily POSITIONAL: a Message belongs to the
rewound span when the last seq seen before it was written exceeds the
marker's to_seq. That mirrors how compaction already folds by physical
position.
"""

import json

from primer.workspace.session import reconstruct_compacted_history


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


def _texts(lines):
    return [p.text for m in reconstruct_compacted_history(lines) for p in m.parts]


def test_plain_history_is_untouched():
    lines = [_rec(1, "user_input"), _msg("user", "a"), _msg("assistant", "b")]
    assert _texts(lines) == ["a", "b"]


def test_rewind_drops_messages_written_after_the_target_seq():
    lines = [_rec(1, "user_input"), _msg("user", "keep"),
             _rec(2, "assistant_token"), _msg("assistant", "drop"),
             _rec(3, "done"),
             _rec(4, "rewind_marker", to_seq=1)]
    assert _texts(lines) == ["keep"]


def test_messages_after_the_rewind_marker_survive():
    """The marker folds what preceded it, never what follows."""
    lines = [_rec(1, "user_input"), _msg("user", "old"),
             _rec(2, "done"),
             _rec(3, "rewind_marker", to_seq=1),
             _rec(4, "user_input"), _msg("user", "new")]
    assert _texts(lines) == ["old", "new"]


def test_repeated_rewinds_nest():
    lines = [_rec(1, "user_input"), _msg("user", "first"),
             _rec(2, "done"),
             _rec(3, "rewind_marker", to_seq=1),
             _rec(4, "user_input"), _msg("user", "second"),
             _rec(5, "done"),
             _rec(6, "rewind_marker", to_seq=4)]
    assert _texts(lines) == ["first", "second"]


def test_compaction_still_folds_everything_before_it():
    lines = [_msg("user", "old"),
             _rec(1, "compaction_marker", summary="summary"),
             _msg("user", "new")]
    assert _texts(lines) == ["summary", "new"]


def test_rewind_after_a_compaction_keeps_the_summary():
    lines = [_msg("user", "old"),
             _rec(1, "compaction_marker", summary="summary"),
             _rec(2, "user_input"), _msg("user", "keep"),
             _rec(3, "assistant_token"), _msg("assistant", "drop"),
             _rec(4, "rewind_marker", to_seq=2)]
    assert _texts(lines) == ["summary", "keep"]


def test_rewind_before_a_compaction_drops_the_summary_too():
    """A compaction inside the rewound span goes with it.

    This is the empty-history case amendment C2 exists to prevent at the
    API layer: the walk is right, and the API must refuse the rewind
    rather than let a turn rebuild from nothing.
    """
    lines = [_msg("user", "old"),
             _rec(5, "compaction_marker", summary="summary"),
             _rec(6, "user_input"), _msg("user", "later"),
             _rec(7, "rewind_marker", to_seq=4)]
    assert _texts(lines) == []
