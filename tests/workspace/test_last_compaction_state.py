"""``last_compaction_state``: what the strategy and the executor remember about the newest compaction.

``tokens_after`` is what the last compaction left the prompt at, so the strategy can avoid summarising its own
summary again before the prompt has grown. It follows the same rules as the history reader: the newest marker wins,
and a marker a rewind cut away no longer stands. ``skip_noted`` says that a run of skipped compactions already has
its ``compaction_note``, so the run is noted once.
"""

import json

from primer.workspace.session import LastCompaction, last_compaction_state


def _tokens_after(lines):
    return last_compaction_state(lines).tokens_after


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload, "created_at": "2026-10-05T00:00:00+00:00"})


def _marker(seq, tokens_after=None, **extra):
    payload = {"summary": "S", **extra}
    if tokens_after is not None:
        payload["tokens_after"] = tokens_after
    return _rec(seq, "compaction_marker", **payload)


def test_a_session_that_was_never_compacted_has_no_figure():
    assert _tokens_after([_rec(1, "user_input"), _msg("user", "q"), _msg("assistant", "a")]) is None
    assert _tokens_after([]) is None


def test_the_figure_is_the_newest_markers():
    lines = [_marker(2, 30_000), _msg("user", "q"), _marker(5, 24_100), _msg("assistant", "a")]
    assert _tokens_after(lines) == 24_100


def test_a_newest_marker_that_recorded_nothing_hides_the_older_figure():
    """An older marker's figure describes a prompt the newer compaction has since replaced."""
    assert _tokens_after([_marker(2, 30_000), _marker(5)]) is None
    assert _tokens_after([_marker(2, 30_000), _marker(5, 0)]) is None
    assert _tokens_after([_marker(2, 30_000), _marker(5, "x")]) is None
    assert _tokens_after([_marker(2, 30_000), _marker(5, True)]) is None


def test_a_rewind_that_cut_the_marker_away_takes_its_figure_with_it():
    lines = [_rec(1, "user_input"), _marker(3, 24_100), _rec(6, "rewind_marker", to_seq=2)]
    assert _tokens_after(lines) is None


def test_a_rewind_to_or_after_the_marker_leaves_it_standing():
    assert _tokens_after([_marker(3, 24_100), _rec(6, "rewind_marker", to_seq=3)]) == 24_100
    assert _tokens_after([_marker(3, 24_100), _rec(6, "rewind_marker", to_seq=5)]) == 24_100


def test_text_that_merely_mentions_a_marker_and_damaged_lines_are_ignored():
    lines = [
        _msg("user", 'what does a "compaction_marker" record say about "rewind_marker"?'),
        "not json but compaction_marker",
        _marker(4, 24_100),
        '["compaction_marker"]',
    ]
    assert _tokens_after(lines) == 24_100


def _note(seq, outcome="skipped", reason="cannot_reach_trigger"):
    return _rec(seq, "compaction_note", outcome=outcome, reason=reason, estimated_tokens=23_000, trigger_tokens=21_400)


class TestSkipNoted:
    def test_a_session_with_no_note_has_none(self):
        assert last_compaction_state([_marker(2, 24_100), _msg("user", "q")]) == LastCompaction(24_100, False)
        assert last_compaction_state([]) == LastCompaction(None, False)

    def test_a_skip_note_marks_the_run_as_noted(self):
        assert last_compaction_state([_note(2)]) == LastCompaction(None, True)
        assert last_compaction_state([_marker(2, 24_100), _msg("user", "q"), _note(5, reason="recently_compacted")]) == \
            LastCompaction(24_100, True)

    def test_the_next_marker_ends_the_run(self):
        """A skip after a new compaction is a new run, and gets its own note."""
        assert last_compaction_state([_note(2), _marker(4, 24_100)]) == LastCompaction(24_100, False)

    def test_only_a_note_of_a_skip_counts(self):
        """An unreducible note is a failure, written every time; it must not silence the first skip note."""
        assert last_compaction_state([_note(2, outcome="unreducible", reason="empty_head")]).skip_noted is False

    def test_a_rewind_that_cut_the_marker_away_starts_over(self):
        lines = [_marker(3, 24_100), _note(4), _rec(6, "rewind_marker", to_seq=2)]
        assert last_compaction_state(lines) == LastCompaction(None, False)

    def test_a_rewind_that_leaves_the_marker_keeps_the_note(self):
        lines = [_marker(3, 24_100), _note(4), _rec(6, "rewind_marker", to_seq=5)]
        assert last_compaction_state(lines) == LastCompaction(24_100, True)
