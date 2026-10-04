"""A compaction marker keeps the tail it was written with (the tier-2 data-loss fix).

Before the fix a marker replaced EVERYTHING physically before it, including the tail
(the last few turns) the compactor had deliberately kept verbatim and the user input
the turn was about to answer. The next load rebuilt ``[summary]`` and nothing else.
The marker now carries ``kept_tail_messages``; the shared reader puts them right after
the summary, ahead of whatever was appended after the marker, and rewinds treat them
as written at the marker's seq.
"""

import json

from primer.workspace.session import reconstruct_compacted_history


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-10-05T00:00:00+00:00"})


def _kept(*pairs):
    return [{"role": role, "parts": [{"type": "text", "text": text}]} for role, text in pairs]


def _marker(seq, summary="SUMMARY", kept=None, summary_after=None):
    payload = {"summary": summary, "replaced_from_seq": 1, "replaced_to_seq": seq - 1}
    if kept is not None:
        payload["kept_tail_messages"] = kept
    if summary_after is not None:
        payload["summary_after"] = summary_after
    return _rec(seq, "compaction_marker", **payload)


def _shown(lines):
    return [(m.role, "".join(p.text for p in m.parts)) for m in reconstruct_compacted_history(lines)]


def test_the_kept_tail_follows_the_summary_and_precedes_what_was_appended_after_the_marker():
    lines = [
        _rec(1, "user_input"), _msg("user", "old question"), _msg("assistant", "old answer"),
        _msg("user", "recent question"), _msg("assistant", "recent answer"), _msg("user", "THE CURRENT QUESTION"),
        _marker(2, kept=_kept(("assistant", "recent answer"), ("user", "THE CURRENT QUESTION"))),
        _msg("assistant", "the reply to the current question"),
    ]
    assert _shown(lines) == [
        ("assistant", "SUMMARY"),
        ("assistant", "recent answer"),
        ("user", "THE CURRENT QUESTION"),
        ("assistant", "the reply to the current question"),
    ]


def test_a_marker_without_a_kept_tail_still_replaces_everything_before_it():
    """Markers written before this change (and by prune-free callers) carry no tail: unchanged."""
    lines = [_rec(1, "user_input"), _msg("user", "folded"), _marker(2), _msg("user", "after")]
    assert _shown(lines) == [("assistant", "SUMMARY"), ("user", "after")]


def test_a_later_marker_supersedes_the_earlier_markers_tail():
    lines = [
        _msg("user", "q1"), _marker(2, "S1", kept=_kept(("user", "tail one"))),
        _msg("assistant", "a2"), _msg("user", "q2"),
        _marker(5, "S2", kept=_kept(("user", "tail two"))),
    ]
    assert _shown(lines) == [("assistant", "S2"), ("user", "tail two")]


def test_a_rewind_after_the_marker_keeps_the_marker_and_its_tail():
    lines = [
        _msg("user", "q1"), _marker(2, kept=_kept(("user", "tail"))),
        _rec(3, "user_input"), _msg("user", "keep this"),
        _rec(4, "assistant_token"), _msg("assistant", "drop this"),
        _rec(5, "rewind_marker", to_seq=3),
    ]
    assert _shown(lines) == [("assistant", "SUMMARY"), ("user", "tail"), ("user", "keep this")]


def test_a_rewind_into_the_span_the_marker_folded_takes_the_summary_and_its_tail_with_it():
    """``check_rewind_target`` refuses this rewind; the reader must still not resurrect the tail from it.

    The folded lines are gone from the visible set, so what is left after the cut is empty: the kept
    tail was written at the marker's seq, which is past the target, and goes with the summary.
    """
    lines = [
        _rec(1, "user_input"), _msg("user", "before"),
        _rec(2, "user_input"), _msg("user", "cut me"),
        _marker(3, kept=_kept(("user", "tail"))),
        _rec(4, "rewind_marker", to_seq=1),
    ]
    assert _shown(lines) == []


def test_an_unreadable_kept_tail_is_dropped_whole_not_half_applied_and_the_model_is_told():
    """A tail with one bad entry could hold a tool call without its result: keep the summary, drop the tail, and say so."""
    from primer.workspace.session import UNREADABLE_TAIL_NOTE

    bad = _kept(("assistant", "ok")) + [{"role": "nonsense", "parts": 7}]
    lines = [_msg("user", "q"), _marker(2, kept=bad), _msg("user", "after")]
    assert _shown(lines) == [("assistant", "SUMMARY"), ("assistant", UNREADABLE_TAIL_NOTE), ("user", "after")]


def test_tool_calls_and_their_results_survive_the_round_trip_as_one_unit():
    kept = [
        {"role": "assistant", "parts": [{"type": "tool_call", "id": "c1", "name": "exec", "arguments": {"command": "ls"}}]},
        {"role": "tool", "parts": [{"type": "tool_result", "id": "c1", "output": "a b c", "error": False}]},
    ]
    shown = reconstruct_compacted_history([_msg("user", "q"), _marker(2, kept=kept)])
    assert [m.role for m in shown] == ["assistant", "assistant", "tool"]
    assert shown[1].parts[0].id == shown[2].parts[0].id == "c1"


def test_a_steer_written_after_the_last_marker_follows_its_tail():
    lines = [_msg("user", "Q"), _marker(2, kept=_kept(("user", "Q"))), _msg("user", "STEER")]
    assert _shown(lines) == [("assistant", "SUMMARY"), ("user", "Q"), ("user", "STEER")]


def test_a_summary_goes_after_the_kept_messages_the_marker_says():
    """summary_after=1: the turn's opening user message stays first, the summary follows it, then the newest round."""
    lines = [
        _msg("user", "old"), _msg("user", "THE QUESTION"),
        _marker(2, "S", kept=_kept(("user", "THE QUESTION"), ("assistant", "newest round")), summary_after=1),
        _msg("user", "STEER"),
    ]
    assert _shown(lines) == [
        ("user", "THE QUESTION"), ("assistant", "S"), ("assistant", "newest round"), ("user", "STEER"),
    ]


def test_a_marker_without_summary_after_puts_the_summary_in_front_as_every_older_marker_did():
    lines = [_msg("user", "q"), _marker(2, "S", kept=_kept(("user", "tail")))]
    assert _shown(lines) == [("assistant", "S"), ("user", "tail")]


def test_a_summary_after_past_the_kept_messages_is_clamped_and_a_bad_value_means_in_front():
    kept = _kept(("user", "a"))
    assert _shown([_marker(2, "S", kept=kept, summary_after=9)]) == [("user", "a"), ("assistant", "S")]
    assert _shown([_marker(2, "S", kept=kept, summary_after="x")]) == [("assistant", "S"), ("user", "a")]
    assert _shown([_marker(2, "S", kept=kept, summary_after=-3)]) == [("assistant", "S"), ("user", "a")]


def test_an_unreadable_tail_ignores_summary_after_so_the_note_follows_the_summary():
    from primer.workspace.session import UNREADABLE_TAIL_NOTE

    bad = _kept(("assistant", "ok")) + [{"role": "nonsense", "parts": 7}]
    assert _shown([_marker(2, "S", kept=bad, summary_after=1)]) == [("assistant", "S"), ("assistant", UNREADABLE_TAIL_NOTE)]
