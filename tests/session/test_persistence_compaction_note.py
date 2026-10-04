"""A compaction that could not reduce the prompt and wrote no marker becomes a compaction_note record."""

from __future__ import annotations

from primer.model.chat import ExtendedEvent, _CompactionNote
from primer.model.workspace_session import SessionMessageKind
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.workspace.session import reconstruct_compacted_history


def _event() -> ExtendedEvent:
    return ExtendedEvent(extended=_CompactionNote(
        outcome="unreducible", reason="protected_over_trigger", estimated_tokens=120_000, trigger_tokens=82_627,
    ))


def test_the_event_becomes_a_compaction_note_record_with_the_verdict() -> None:
    rec = translate_stream_event(_event(), _CoalesceState())
    assert rec is not None
    assert rec.kind is SessionMessageKind.COMPACTION_NOTE
    assert rec.payload == {
        "outcome": "unreducible", "reason": "protected_over_trigger",
        "estimated_tokens": 120_000, "trigger_tokens": 82_627,
    }


def test_the_record_is_never_prompt_history() -> None:
    rec = translate_stream_event(_event(), _CoalesceState())
    assert rec is not None
    assert reconstruct_compacted_history([rec.model_dump_json()]) == []
