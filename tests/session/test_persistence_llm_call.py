"""S7: llm_call events become llm_call records, node-attributed for free."""

from __future__ import annotations

from primer.model.chat import ExtendedEvent, _GraphNodeEvent, _LlmCall
from primer.model.workspace_session import SessionMessageKind
from primer.session.persistence import _CoalesceState, translate_stream_event


def _event() -> ExtendedEvent:
    return ExtendedEvent(
        extended=_LlmCall(
            profile_id="prof-1",
            provider_id="prov-1",
            model="m-1",
            input_tokens=11,
            output_tokens=7,
            duration_ms=250,
            status="ok",
        )
    )


def test_event_becomes_an_llm_call_record():
    rec = translate_stream_event(_event(), _CoalesceState())
    assert rec is not None
    assert rec.kind is SessionMessageKind.LLM_CALL
    assert rec.payload == {
        "profile_id": "prof-1",
        "provider_id": "prov-1",
        "model": "m-1",
        "input_tokens": 11,
        "output_tokens": 7,
        "duration_ms": 250,
        "status": "ok",
    }
    assert rec.node_id is None


def test_graph_wrapped_event_carries_the_node_id():
    inner = _event()
    wrapped = ExtendedEvent(
        extended=_GraphNodeEvent(
            node_id="n-1",
            iteration=0,
            inner_type=inner.type,
            inner_payload=inner.model_dump(mode="json"),
        )
    )
    rec = translate_stream_event(wrapped, _CoalesceState())
    assert rec is not None
    assert rec.kind is SessionMessageKind.LLM_CALL
    assert rec.node_id == "n-1"


def test_text_buffer_is_not_flushed_by_an_llm_call():
    """Only ToolCallEnd / Done flush the coalesced assistant text."""
    from primer.model.chat import TextDelta

    state = _CoalesceState()
    translate_stream_event(TextDelta(text="partial", index=0), state)
    translate_stream_event(_event(), state)
    assert state.text_buffers[None] == "partial"


def test_llm_call_records_are_excluded_from_prompt_rebuild():
    """Spec section 5: display/derivation only, never LLM history.

    messages.jsonl interleaves role/parts Message lines with seq/kind
    event-log records; the shared history reader admits only the former
    (primer/workspace/session.py:174), so the exclusion is structural.
    Pinned here so a future reader change cannot start replaying model
    telemetry back into the prompt.
    """
    import json

    from primer.workspace.session import reconstruct_compacted_history

    rec = translate_stream_event(_event(), _CoalesceState())
    assert rec is not None
    lines = [
        json.dumps({"role": "user", "parts": [{"type": "text", "text": "hi"}]}),
        rec.model_dump_json(),
    ]
    history = reconstruct_compacted_history(lines)
    assert len(history) == 1
    assert history[0].role == "user"
