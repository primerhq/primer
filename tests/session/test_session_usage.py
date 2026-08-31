"""Per-session usage folded from the record ledger (S1 P2 T14).

Spec section 7. The records are already the ledger, so usage is folded
at read time rather than kept in a counter column: a second source of
truth would need reconciling on every rewind and compaction.
"""

import json

from primer.session.usage import session_usage


def _done(seq, inp, out):
    return json.dumps({
        "seq": seq, "kind": "done",
        "payload": {"usage": {"input_tokens": inp, "output_tokens": out}},
        "created_at": "2026-08-16T00:00:00+00:00",
    })


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


def test_empty_log_is_all_zeros():
    u = session_usage([])
    assert (u.turns, u.total_input_tokens, u.last_output_tokens) == (0, 0, 0)


def test_totals_accumulate_and_last_is_the_newest():
    u = session_usage([_done(3, 100, 10), _done(6, 200, 20)])
    assert u.turns == 2
    assert u.total_input_tokens == 300
    assert u.total_output_tokens == 30
    assert u.last_input_tokens == 200
    assert u.last_output_tokens == 20


def test_a_done_without_usage_counts_as_a_turn_but_adds_nothing():
    u = session_usage([_done(3, 100, 10), _rec(6, "done")])
    assert u.turns == 2
    assert u.total_input_tokens == 100
    assert u.last_input_tokens == 100  # the last DONE that HAD usage


def test_rewound_turns_stop_counting():
    """Folding the visible set is what gives this for free: a counter
    column would need a compensating write."""
    u = session_usage([_done(3, 100, 10), _done(6, 200, 20),
                       _rec(7, "rewind_marker", to_seq=3)])
    assert u.turns == 1
    assert u.total_input_tokens == 100


def test_compaction_folds_prior_usage_away():
    u = session_usage([
        _done(3, 100, 10),
        _rec(4, "compaction_marker", summary="s", replaced_to_seq=3),
        _done(7, 50, 5),
    ])
    assert u.turns == 1
    assert u.total_input_tokens == 50


def test_optional_token_kinds_are_summed_when_present():
    lines = [json.dumps({
        "seq": 3, "kind": "done",
        "payload": {"usage": {"input_tokens": 10, "output_tokens": 2,
                              "cached_input_tokens": 7,
                              "reasoning_tokens": 5}},
        "created_at": "2026-08-16T00:00:00+00:00",
    })]
    u = session_usage(lines)
    assert u.total_cached_input_tokens == 7
    assert u.total_reasoning_tokens == 5
