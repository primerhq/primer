"""``flush_partial_output``: make what a stopped turn had already streamed durable.

Text and reasoning coalesce in ``_CoalesceState`` and become records only at a tool call or at Done.
A turn cut short by Stop or Cancel reaches neither, so without this the partial answer existed only
in the live view and vanished on refresh.
"""

from __future__ import annotations

from primer.model.chat import Done, ReasoningDelta, TextDelta
from primer.model.workspace_session import SessionMessageKind
from primer.session.persistence import _CoalesceState, flush_partial_output, translate_stream_event
from primer.tap.delta import KIND_REASONING, KIND_TEXT, part_id


class _Sink:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def on_delta(self, pid: str, kind: str, delta: str) -> None:
        pass

    def close(self, pid: str) -> None:
        self.closed.append(pid)


def _feed(state: _CoalesceState, *events, turn_no: int = 3) -> None:
    for event in events:
        assert translate_stream_event(event, state, turn_no=turn_no) is None   # coalescing, nothing emitted yet


def test_buffered_text_becomes_one_assistant_token_record() -> None:
    state = _CoalesceState()
    _feed(state, TextDelta(text="par", index=0), TextDelta(text="tial", index=0))

    records = flush_partial_output(state, turn_no=3)

    assert [r.kind for r in records] == [SessionMessageKind.ASSISTANT_TOKEN]
    assert records[0].payload["text"] == "partial"
    assert records[0].payload["part_id"] == part_id(None, KIND_TEXT, 3)


def test_reasoning_comes_before_the_answer_as_at_every_other_flush_point() -> None:
    state = _CoalesceState()
    _feed(state, ReasoningDelta(text="thinking", index=0), TextDelta(text="answer", index=0))

    records = flush_partial_output(state, turn_no=3)

    assert [r.kind for r in records] == [SessionMessageKind.REASONING, SessionMessageKind.ASSISTANT_TOKEN]
    assert [r.payload["text"] for r in records] == ["thinking", "answer"]


def test_every_node_is_flushed_and_attributed_to_its_own_node() -> None:
    state = _CoalesceState()
    state.text_buffers["node-a"] = "from a"
    state.text_buffers["node-b"] = "from b"

    records = flush_partial_output(state, turn_no=0)

    assert sorted((r.node_id, r.payload["text"]) for r in records) == [("node-a", "from a"), ("node-b", "from b")]


def test_the_buffers_are_drained_so_nothing_is_written_twice() -> None:
    state = _CoalesceState()
    _feed(state, TextDelta(text="once", index=0))

    first = flush_partial_output(state, turn_no=3)
    second = flush_partial_output(state, turn_no=3)
    done = translate_stream_event(Done(stop_reason="stop", raw_reason="stop"), state, turn_no=3)

    assert len(first) == 1 and second == []
    assert done.kind == SessionMessageKind.DONE, "a later Done must not replay the flushed text"


def test_the_live_parts_are_closed_so_the_client_stops_showing_them_as_streaming() -> None:
    state = _CoalesceState()
    _feed(state, TextDelta(text="x", index=0))
    sink = _Sink()

    flush_partial_output(state, delta_sink=sink, turn_no=3)

    assert sink.closed == [part_id(None, KIND_TEXT, 3), part_id(None, KIND_REASONING, 3)]


def test_nothing_buffered_means_no_records_and_nothing_to_close() -> None:
    sink = _Sink()

    assert flush_partial_output(_CoalesceState(), delta_sink=sink, turn_no=3) == []
    assert sink.closed == []


def test_a_stopped_turn_has_no_final_result_to_relay() -> None:
    state = _CoalesceState()
    _feed(state, TextDelta(text="cut off", index=0))

    flush_partial_output(state, turn_no=3)

    assert state.last_assistant_token is None
