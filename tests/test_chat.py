"""Unit tests for the new functionality added to matrix/model/chat.py:

* :class:`ToolCallPart` — assistant tool-call part for round-tripping.
* :class:`ToolResultPart` — tool-result part for the next turn.
* ``"tool"`` role on :class:`Message`.
* :func:`output_to_message` — default converter from output stream events
  to an assistant input :class:`Message`.

Plus regression coverage for the parts of the existing :data:`Part`
union that interact with the new types.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from matrix.model.chat import (
    AudioPart,
    Citation,
    DocumentPart,
    Done,
    Error,
    ExtendedEvent,
    ExtendedPart,
    ImagePart,
    Logprobs,
    MediaDelta,
    Message,
    Part,
    RawReasoningDelta,
    ReasoningDelta,
    RefusalDelta,
    SafetyRatings,
    ServerToolCallStart,
    StreamStart,
    TextDelta,
    TextPart,
    TokenLogprob,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    Usage,
    output_to_message,
)
from matrix.model.common import Describeable, Identifiable


# ============================================================================
# ToolCallPart
# ============================================================================


class TestToolCallPart:
    def test_construction_minimal(self):
        part = ToolCallPart(id="tc_1", name="f", arguments={})
        assert part.type == "tool_call"
        assert part.id == "tc_1"
        assert part.name == "f"
        assert part.arguments == {}

    def test_default_type_is_tool_call(self):
        part = ToolCallPart(id="x", name="y", arguments={})
        assert part.type == "tool_call"

    def test_complex_arguments(self):
        args = {
            "string": "value",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "nested": {"deep": {"value": "x"}},
        }
        part = ToolCallPart(id="x", name="y", arguments=args)
        assert part.arguments == args

    def test_id_required(self):
        with pytest.raises(ValidationError):
            ToolCallPart(name="f", arguments={})

    def test_id_min_length(self):
        with pytest.raises(ValidationError):
            ToolCallPart(id="", name="f", arguments={})

    def test_name_required(self):
        with pytest.raises(ValidationError):
            ToolCallPart(id="x", arguments={})

    def test_name_min_length(self):
        with pytest.raises(ValidationError):
            ToolCallPart(id="x", name="", arguments={})

    def test_arguments_required(self):
        with pytest.raises(ValidationError):
            ToolCallPart(id="x", name="y")

    def test_arguments_must_be_dict(self):
        with pytest.raises(ValidationError):
            ToolCallPart(id="x", name="y", arguments="not a dict")

    def test_explicit_type_accepted(self):
        # The literal default doesn't prevent passing the same value explicitly.
        part = ToolCallPart(type="tool_call", id="x", name="y", arguments={})
        assert part.type == "tool_call"

    def test_wrong_type_rejected(self):
        with pytest.raises(ValidationError):
            ToolCallPart(type="something_else", id="x", name="y", arguments={})


# ============================================================================
# ToolResultPart
# ============================================================================


class TestToolResultPart:
    def test_construction_minimal(self):
        part = ToolResultPart(id="tc_1", output="result")
        assert part.type == "tool_result"
        assert part.id == "tc_1"
        assert part.output == "result"
        assert part.error is False  # default

    def test_error_flag_true(self):
        part = ToolResultPart(id="tc_1", output="failed", error=True)
        assert part.error is True

    def test_error_flag_false_explicit(self):
        part = ToolResultPart(id="tc_1", output="ok", error=False)
        assert part.error is False

    def test_id_required(self):
        with pytest.raises(ValidationError):
            ToolResultPart(output="x")

    def test_id_min_length(self):
        with pytest.raises(ValidationError):
            ToolResultPart(id="", output="x")

    def test_output_required(self):
        with pytest.raises(ValidationError):
            ToolResultPart(id="x")

    def test_output_can_be_empty_string(self):
        # An empty output is valid (some tools return nothing on success).
        part = ToolResultPart(id="x", output="")
        assert part.output == ""

    def test_output_must_be_string(self):
        with pytest.raises(ValidationError):
            ToolResultPart(id="x", output={"not": "string"})

    def test_long_output(self):
        long_output = "x" * 100_000
        part = ToolResultPart(id="x", output=long_output)
        assert len(part.output) == 100_000


# ============================================================================
# Part union — discrimination of new tool round-trip members
# ============================================================================


class TestPartUnionWithToolParts:
    @pytest.fixture
    def adapter(self) -> TypeAdapter:
        return TypeAdapter(Part)

    def test_tool_call_discriminator(self, adapter):
        parsed = adapter.validate_python(
            {"type": "tool_call", "id": "tc_1", "name": "f", "arguments": {}}
        )
        assert isinstance(parsed, ToolCallPart)

    def test_tool_result_discriminator(self, adapter):
        parsed = adapter.validate_python(
            {"type": "tool_result", "id": "tc_1", "output": "x"}
        )
        assert isinstance(parsed, ToolResultPart)

    def test_existing_text_still_resolves(self, adapter):
        parsed = adapter.validate_python({"type": "text", "text": "hi"})
        assert isinstance(parsed, TextPart)

    def test_existing_image_still_resolves(self, adapter):
        parsed = adapter.validate_python({"type": "image", "url": "https://x"})
        assert isinstance(parsed, ImagePart)

    def test_existing_document_still_resolves(self, adapter):
        parsed = adapter.validate_python({"type": "document", "url": "https://x"})
        assert isinstance(parsed, DocumentPart)

    def test_extended_wrapper_still_resolves(self, adapter):
        parsed = adapter.validate_python(
            {"type": "extended", "extended": {"type": "audio", "url": "https://x"}}
        )
        assert isinstance(parsed, ExtendedPart)
        assert isinstance(parsed.extended, AudioPart)

    def test_unknown_type_rejected(self, adapter):
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "bogus", "x": 1})

    def test_extended_wrapper_audio_video_only(self, adapter):
        # Tool parts must be at the top level, not wrapped under ExtendedPart's inner union
        with pytest.raises(ValidationError):
            adapter.validate_python(
                {"type": "extended", "extended": {"type": "tool_call", "id": "x", "name": "y", "arguments": {}}}
            )


# ============================================================================
# Message with the new "tool" role
# ============================================================================


class TestMessageRole:
    def test_user_role_unchanged(self):
        msg = Message(role="user", parts=[TextPart(text="hi")])
        assert msg.role == "user"

    def test_assistant_role_unchanged(self):
        msg = Message(role="assistant", parts=[TextPart(text="hi")])
        assert msg.role == "assistant"

    def test_system_role_unchanged(self):
        msg = Message(role="system", parts=[TextPart(text="hi")])
        assert msg.role == "system"

    def test_tool_role_accepted(self):
        msg = Message(role="tool", parts=[ToolResultPart(id="tc_1", output="ok")])
        assert msg.role == "tool"

    def test_unknown_role_rejected(self):
        with pytest.raises(ValidationError):
            Message(role="bogus", parts=[TextPart(text="hi")])

    def test_assistant_with_tool_calls(self):
        msg = Message(
            role="assistant",
            parts=[
                TextPart(text="Let me check"),
                ToolCallPart(id="tc_1", name="get_weather", arguments={"city": "SF"}),
            ],
        )
        assert len(msg.parts) == 2
        assert isinstance(msg.parts[0], TextPart)
        assert isinstance(msg.parts[1], ToolCallPart)

    def test_tool_role_with_multiple_results(self):
        msg = Message(
            role="tool",
            parts=[
                ToolResultPart(id="tc_1", output="r1"),
                ToolResultPart(id="tc_2", output="r2"),
            ],
        )
        assert len(msg.parts) == 2

    def test_tool_role_with_error_result(self):
        msg = Message(
            role="tool",
            parts=[ToolResultPart(id="tc_1", output="denied", error=True)],
        )
        assert msg.parts[0].error is True


class TestMessageParts:
    def test_message_round_trip_assistant_with_tool_call(self):
        msg = Message(
            role="assistant",
            parts=[
                TextPart(text="Calling tool"),
                ToolCallPart(id="tc_1", name="f", arguments={"a": 1}),
            ],
        )
        json_str = msg.model_dump_json()
        restored = Message.model_validate_json(json_str)
        assert restored.role == "assistant"
        assert len(restored.parts) == 2
        assert isinstance(restored.parts[0], TextPart)
        assert isinstance(restored.parts[1], ToolCallPart)
        assert restored.parts[1].arguments == {"a": 1}

    def test_message_round_trip_tool_result(self):
        msg = Message(
            role="tool",
            parts=[ToolResultPart(id="tc_1", output="ok")],
        )
        restored = Message.model_validate_json(msg.model_dump_json())
        assert restored.role == "tool"
        assert isinstance(restored.parts[0], ToolResultPart)
        assert restored.parts[0].id == "tc_1"
        assert restored.parts[0].output == "ok"
        assert restored.parts[0].error is False


# ============================================================================
# output_to_message — single-event-type cases
# ============================================================================


class TestOutputToMessageText:
    def test_returns_assistant_role(self):
        msg = output_to_message([TextDelta(text="x", index=0)])
        assert msg.role == "assistant"

    def test_single_text_delta(self):
        msg = output_to_message([TextDelta(text="hello", index=0)])
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], TextPart)
        assert msg.parts[0].text == "hello"

    def test_multiple_text_deltas_same_index_concatenate(self):
        events = [
            TextDelta(text="Hello", index=0),
            TextDelta(text=", ", index=0),
            TextDelta(text="world", index=0),
            TextDelta(text="!", index=0),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == "Hello, world!"

    def test_multiple_text_deltas_different_indices(self):
        events = [
            TextDelta(text="alpha", index=0),
            TextDelta(text="beta", index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 2
        assert msg.parts[0].text == "alpha"
        assert msg.parts[1].text == "beta"

    def test_text_indices_in_non_monotonic_order(self):
        # First-appearance order, not numeric order.
        events = [
            TextDelta(text="b", index=5),
            TextDelta(text="a", index=2),
            TextDelta(text="c", index=2),  # extends index 2
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 2
        assert msg.parts[0].text == "b"  # index 5, appeared first
        assert msg.parts[1].text == "ac"  # index 2, second appearance, two deltas

    def test_empty_text_delta_strings(self):
        # Empty strings still register the index but contribute nothing.
        events = [
            TextDelta(text="", index=0),
            TextDelta(text="content", index=0),
        ]
        msg = output_to_message(events)
        assert msg.parts[0].text == "content"

    def test_only_empty_text_deltas(self):
        # Empty text still creates a part (the index appeared).
        events = [TextDelta(text="", index=0)]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == ""


# ============================================================================
# output_to_message — tool call cases
# ============================================================================


class TestOutputToMessageToolCalls:
    def test_single_tool_call(self):
        events = [
            ToolCallStart(id="tc_1", name="get_weather", index=0),
            ToolCallEnd(id="tc_1", arguments={"city": "SF"}, index=0),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], ToolCallPart)
        assert msg.parts[0].id == "tc_1"
        assert msg.parts[0].name == "get_weather"
        assert msg.parts[0].arguments == {"city": "SF"}

    def test_tool_call_delta_ignored(self):
        # The End event carries the parsed args; Delta events are ignored.
        events = [
            ToolCallStart(id="tc_1", name="f", index=0),
            ToolCallDelta(id="tc_1", arguments_delta='{"a"', index=0),
            ToolCallDelta(id="tc_1", arguments_delta=': 1}', index=0),
            ToolCallEnd(id="tc_1", arguments={"a": 1}, index=0),
        ]
        msg = output_to_message(events)
        assert msg.parts[0].arguments == {"a": 1}

    def test_tool_call_without_end_uses_empty_args(self):
        events = [ToolCallStart(id="tc_1", name="f", index=0)]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].name == "f"
        assert msg.parts[0].arguments == {}

    def test_tool_call_without_end_with_deltas_uses_empty_args(self):
        # Deltas alone don't supply parsed args.
        events = [
            ToolCallStart(id="tc_1", name="f", index=0),
            ToolCallDelta(id="tc_1", arguments_delta='{"a": 1}', index=0),
        ]
        msg = output_to_message(events)
        assert msg.parts[0].arguments == {}

    def test_orphan_tool_call_end_ignored(self):
        # An End without a preceding Start has no name to use; should be dropped.
        events = [
            ToolCallEnd(id="tc_orphan", arguments={"a": 1}, index=0),
            TextDelta(text="hi", index=1),
        ]
        msg = output_to_message(events)
        # Only the text part survives.
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], TextPart)

    def test_multiple_tool_calls_in_order(self):
        events = [
            ToolCallStart(id="tc_1", name="f1", index=0),
            ToolCallStart(id="tc_2", name="f2", index=1),
            ToolCallEnd(id="tc_1", arguments={"a": 1}, index=0),
            ToolCallEnd(id="tc_2", arguments={"b": 2}, index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 2
        assert msg.parts[0].id == "tc_1"
        assert msg.parts[0].arguments == {"a": 1}
        assert msg.parts[1].id == "tc_2"
        assert msg.parts[1].arguments == {"b": 2}

    def test_tool_call_complex_arguments(self):
        complex_args = {
            "string": "value",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, "x"],
            "nested": {"deep": {"value": "x"}},
        }
        events = [
            ToolCallStart(id="tc_1", name="f", index=0),
            ToolCallEnd(id="tc_1", arguments=complex_args, index=0),
        ]
        msg = output_to_message(events)
        assert msg.parts[0].arguments == complex_args

    def test_tool_call_empty_arguments(self):
        events = [
            ToolCallStart(id="tc_1", name="now", index=0),
            ToolCallEnd(id="tc_1", arguments={}, index=0),
        ]
        msg = output_to_message(events)
        assert msg.parts[0].arguments == {}


# ============================================================================
# output_to_message — interleaved text and tool calls
# ============================================================================


class TestOutputToMessageInterleaved:
    def test_text_then_tool(self):
        events = [
            TextDelta(text="thinking", index=0),
            ToolCallStart(id="tc_1", name="f", index=1),
            ToolCallEnd(id="tc_1", arguments={}, index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 2
        assert isinstance(msg.parts[0], TextPart)
        assert msg.parts[0].text == "thinking"
        assert isinstance(msg.parts[1], ToolCallPart)

    def test_tool_then_text(self):
        events = [
            ToolCallStart(id="tc_1", name="f", index=0),
            ToolCallEnd(id="tc_1", arguments={}, index=0),
            TextDelta(text="done", index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 2
        assert isinstance(msg.parts[0], ToolCallPart)
        assert isinstance(msg.parts[1], TextPart)
        assert msg.parts[1].text == "done"

    def test_text_tool_text_interleaved(self):
        events = [
            TextDelta(text="a", index=0),
            ToolCallStart(id="tc_1", name="f", index=1),
            TextDelta(text="b", index=0),  # extends index 0
            ToolCallEnd(id="tc_1", arguments={}, index=1),
            TextDelta(text="c", index=2),  # new text block after the tool call
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 3
        assert msg.parts[0].text == "ab"
        assert isinstance(msg.parts[1], ToolCallPart)
        assert msg.parts[2].text == "c"

    def test_two_text_two_tools_interleaved(self):
        events = [
            TextDelta(text="text1", index=0),
            ToolCallStart(id="tc_1", name="f1", index=1),
            ToolCallEnd(id="tc_1", arguments={"x": 1}, index=1),
            TextDelta(text="text2", index=2),
            ToolCallStart(id="tc_2", name="f2", index=3),
            ToolCallEnd(id="tc_2", arguments={"y": 2}, index=3),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 4
        assert msg.parts[0].text == "text1"
        assert msg.parts[1].id == "tc_1"
        assert msg.parts[2].text == "text2"
        assert msg.parts[3].id == "tc_2"

    def test_tool_call_streamed_around_text(self):
        # Mimics a real provider: ToolCallStart fires, then text deltas
        # stream alongside it (e.g. reasoning models), then ToolCallEnd.
        events = [
            ToolCallStart(id="tc_1", name="f", index=1),
            TextDelta(text="meanwhile", index=0),  # text block opens AFTER tool call
            ToolCallDelta(id="tc_1", arguments_delta='{}', index=1),
            ToolCallEnd(id="tc_1", arguments={}, index=1),
        ]
        msg = output_to_message(events)
        # Tool call appeared first; text block second.
        assert len(msg.parts) == 2
        assert isinstance(msg.parts[0], ToolCallPart)
        assert isinstance(msg.parts[1], TextPart)
        assert msg.parts[1].text == "meanwhile"


# ============================================================================
# output_to_message — ignored event types
# ============================================================================


class TestOutputToMessageIgnoredEvents:
    def test_stream_start_ignored(self):
        events = [
            StreamStart(model="gpt-4", request_id="resp_x"),
            TextDelta(text="hi", index=0),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == "hi"

    def test_done_ignored(self):
        events = [
            TextDelta(text="hi", index=0),
            Done(stop_reason="stop", raw_reason="end_turn"),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_error_event_ignored(self):
        events = [
            TextDelta(text="partial", index=0),
            Error(message="oops", fatal=True),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == "partial"

    def test_usage_ignored(self):
        events = [
            TextDelta(text="hi", index=0),
            Usage(input_tokens=10, output_tokens=5, cumulative=False),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_reasoning_delta_ignored(self):
        # Reasoning is provider-specific to round-trip; default converter drops it.
        events = [
            ReasoningDelta(text="thinking out loud", index=0),
            TextDelta(text="answer", index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], TextPart)
        assert msg.parts[0].text == "answer"

    def test_media_delta_ignored(self):
        events = [
            TextDelta(text="here is audio", index=0),
            MediaDelta(kind="audio", data=b"binary_audio", mime_type="audio/mpeg", index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == "here is audio"

    def test_extended_event_citation_ignored(self):
        events = [
            TextDelta(text="cited claim", index=0),
            ExtendedEvent(extended=Citation(source_url="https://example.com", index=0)),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_extended_event_refusal_ignored(self):
        events = [
            TextDelta(text="some content", index=0),
            ExtendedEvent(extended=RefusalDelta(text="cannot comply", index=1)),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_extended_event_logprobs_ignored(self):
        events = [
            TextDelta(text="x", index=0),
            ExtendedEvent(
                extended=Logprobs(
                    tokens=[TokenLogprob(token="x", logprob=-0.5)], index=0
                )
            ),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_extended_event_safety_ratings_ignored(self):
        events = [
            TextDelta(text="x", index=0),
            ExtendedEvent(extended=SafetyRatings(ratings={"HARM": "LOW"})),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1

    def test_extended_event_raw_reasoning_ignored(self):
        events = [
            ExtendedEvent(extended=RawReasoningDelta(text="raw trace", index=0)),
            TextDelta(text="visible", index=1),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1
        assert msg.parts[0].text == "visible"

    def test_extended_event_server_tool_call_ignored(self):
        events = [
            TextDelta(text="searched the web", index=0),
            ExtendedEvent(
                extended=ServerToolCallStart(id="st_1", tool_name="web_search", index=1)
            ),
        ]
        msg = output_to_message(events)
        assert len(msg.parts) == 1


# ============================================================================
# output_to_message — error cases
# ============================================================================


class TestOutputToMessageErrors:
    def test_empty_events_raises(self):
        with pytest.raises(ValueError, match="no convertible events"):
            output_to_message([])

    def test_only_lifecycle_events_raises(self):
        events = [
            StreamStart(model="gpt-4"),
            Done(stop_reason="stop", raw_reason="end_turn"),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_telemetry_events_raises(self):
        events = [
            Usage(input_tokens=10, output_tokens=0, cumulative=False),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_reasoning_events_raises(self):
        events = [
            ReasoningDelta(text="thinking", index=0),
            ReasoningDelta(text=" more", index=0),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_media_events_raises(self):
        events = [
            MediaDelta(kind="image", data=b"png", mime_type="image/png", index=0),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_extended_events_raises(self):
        events = [
            ExtendedEvent(extended=Citation(source_url="https://x", index=0)),
            ExtendedEvent(extended=RefusalDelta(text="no", index=0)),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_orphan_tool_call_end_raises(self):
        # End without a Start cannot form a complete ToolCallPart.
        events = [
            ToolCallEnd(id="tc_orphan", arguments={"a": 1}, index=0),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)

    def test_only_tool_call_delta_raises(self):
        # Delta alone without Start has no name; nothing to emit.
        events = [
            ToolCallDelta(id="tc_x", arguments_delta='{"a": 1}', index=0),
        ]
        with pytest.raises(ValueError):
            output_to_message(events)


# ============================================================================
# output_to_message — input shape flexibility
# ============================================================================


class TestOutputToMessageInputShapes:
    def test_accepts_list(self):
        msg = output_to_message([TextDelta(text="hi", index=0)])
        assert msg.parts[0].text == "hi"

    def test_accepts_tuple(self):
        msg = output_to_message((TextDelta(text="hi", index=0),))
        assert msg.parts[0].text == "hi"

    def test_accepts_generator(self):
        def gen():
            yield TextDelta(text="from", index=0)
            yield TextDelta(text=" gen", index=0)

        msg = output_to_message(gen())
        assert msg.parts[0].text == "from gen"

    def test_accepts_iterator(self):
        events = iter([TextDelta(text="iter", index=0)])
        msg = output_to_message(events)
        assert msg.parts[0].text == "iter"


# ============================================================================
# Integration: full round-trip through JSON serialisation
# ============================================================================


class TestOutputToMessageRoundTrip:
    def test_roundtrip_through_json(self):
        events = [
            TextDelta(text="result: ", index=0),
            ToolCallStart(id="tc_1", name="lookup", index=1),
            ToolCallEnd(id="tc_1", arguments={"q": "hi"}, index=1),
        ]
        msg = output_to_message(events)
        json_str = msg.model_dump_json()
        restored = Message.model_validate_json(json_str)
        assert restored.role == "assistant"
        assert len(restored.parts) == 2
        assert isinstance(restored.parts[0], TextPart)
        assert restored.parts[0].text == "result: "
        assert isinstance(restored.parts[1], ToolCallPart)
        assert restored.parts[1].id == "tc_1"
        assert restored.parts[1].name == "lookup"
        assert restored.parts[1].arguments == {"q": "hi"}

    def test_full_chat_history_round_trip(self):
        # Simulate a realistic two-turn flow:
        #   turn 1: user -> assistant emits text + tool call
        #   turn 2: tool result -> assistant emits text
        history: list[Message] = []

        # User turn
        history.append(Message(role="user", parts=[TextPart(text="weather in SF?")]))

        # First assistant turn — text + tool call
        events_1 = [
            TextDelta(text="Let me check.", index=0),
            ToolCallStart(id="tc_1", name="get_weather", index=1),
            ToolCallEnd(id="tc_1", arguments={"city": "SF"}, index=1),
        ]
        history.append(output_to_message(events_1))

        # Tool result (caller-constructed)
        history.append(
            Message(
                role="tool",
                parts=[ToolResultPart(id="tc_1", output="72F sunny")],
            )
        )

        # Second assistant turn — text only
        events_2 = [TextDelta(text="It's 72F and sunny in SF.", index=0)]
        history.append(output_to_message(events_2))

        assert len(history) == 4
        assert history[0].role == "user"
        assert history[1].role == "assistant"
        assert isinstance(history[1].parts[1], ToolCallPart)
        assert history[2].role == "tool"
        assert isinstance(history[2].parts[0], ToolResultPart)
        assert history[3].role == "assistant"

        # Whole history must round-trip through JSON.
        for msg in history:
            restored = Message.model_validate_json(msg.model_dump_json())
            assert restored.role == msg.role
            assert len(restored.parts) == len(msg.parts)


# ============================================================================
# Tool — composes from Describeable, adds JSON Schema for arguments
# ============================================================================


class TestTool:
    def test_construction_minimal(self):
        t = Tool(
            id="get_weather",
            description="Get weather for a city.",
            toolset_id="weather_toolset",
            schema={"type": "object"},
        )
        assert t.id == "get_weather"
        assert t.description == "Get weather for a city."
        assert t.toolset_id == "weather_toolset"
        assert t.schema == {"type": "object"}

    def test_inherits_from_describeable(self):
        # Tool is a Describeable, which is an Identifiable.
        t = Tool(id="x", description="y", toolset_id="ts", schema={"type": "object"})
        assert isinstance(t, Describeable)
        assert isinstance(t, Identifiable)

    def test_id_required(self):
        with pytest.raises(ValidationError):
            Tool(description="y", toolset_id="ts", schema={"type": "object"})

    def test_id_min_length(self):
        # Identifiable enforces min_length=1 on id.
        with pytest.raises(ValidationError):
            Tool(id="", description="y", toolset_id="ts", schema={"type": "object"})

    def test_description_required(self):
        with pytest.raises(ValidationError):
            Tool(id="x", toolset_id="ts", schema={"type": "object"})

    def test_toolset_id_required(self):
        with pytest.raises(ValidationError):
            Tool(id="x", description="y", schema={"type": "object"})

    def test_toolset_id_min_length(self):
        with pytest.raises(ValidationError):
            Tool(id="x", description="y", toolset_id="", schema={"type": "object"})

    def test_toolset_id_must_be_string(self):
        with pytest.raises(ValidationError):
            Tool(id="x", description="y", toolset_id=123, schema={"type": "object"})

    def test_schema_required(self):
        with pytest.raises(ValidationError):
            Tool(id="x", description="y", toolset_id="ts")

    def test_schema_must_be_dict(self):
        with pytest.raises(ValidationError):
            Tool(id="x", description="y", toolset_id="ts", schema="not a dict")

    def test_complex_schema(self):
        complex_schema = {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "units": {"type": "string", "enum": ["c", "f"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["city"],
            "additionalProperties": False,
        }
        t = Tool(
            id="weather",
            description="weather",
            toolset_id="weather_toolset",
            schema=complex_schema,
        )
        assert t.schema == complex_schema

    def test_empty_schema_dict_accepted(self):
        # An empty dict is still a valid (if useless) JSON Schema.
        t = Tool(id="x", description="y", toolset_id="ts", schema={})
        assert t.schema == {}

    def test_json_round_trip(self):
        t = Tool(
            id="lookup",
            description="Look something up.",
            toolset_id="search_toolset",
            schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        restored = Tool.model_validate_json(t.model_dump_json())
        assert restored.id == t.id
        assert restored.description == t.description
        assert restored.toolset_id == t.toolset_id
        assert restored.schema == t.schema

    def test_json_serialised_uses_schema_key(self):
        # The Python attribute is `schema`; the JSON key is also "schema".
        t = Tool(id="x", description="y", toolset_id="ts", schema={"type": "object"})
        dumped = t.model_dump()
        assert "schema" in dumped
        assert dumped["schema"] == {"type": "object"}

    def test_json_serialised_includes_toolset_id_key(self):
        t = Tool(id="x", description="y", toolset_id="my_toolset", schema={})
        dumped = t.model_dump()
        assert "toolset_id" in dumped
        assert dumped["toolset_id"] == "my_toolset"

    def test_pydantic_model_json_schema_via_callable(self):
        # Callers can derive a Pydantic class's schema and pass it.
        class WeatherArgs(BaseModel):
            city: str

        t = Tool(
            id="weather",
            description="Get weather",
            toolset_id="weather_toolset",
            schema=WeatherArgs.model_json_schema(),
        )
        assert "properties" in t.schema
        assert "city" in t.schema["properties"]

    def test_multiple_tools_can_share_toolset_id(self):
        # Many tools may belong to the same toolset (the common case for MCP).
        t1 = Tool(id="t1", description="x", toolset_id="shared", schema={})
        t2 = Tool(id="t2", description="y", toolset_id="shared", schema={})
        assert t1.toolset_id == t2.toolset_id == "shared"
        assert t1.id != t2.id


class TestToolCallResult:
    """ToolCallResult is the return type of ToolsetProvider.call."""

    def test_minimal_result_has_default_is_error_false_and_no_extended(self) -> None:
        from matrix.model.chat import ToolCallResult

        r = ToolCallResult(output="hello")

        assert r.output == "hello"
        assert r.is_error is False
        assert r.extended is None

    def test_error_result_is_distinct_from_success(self) -> None:
        from matrix.model.chat import ToolCallResult

        r = ToolCallResult(output="boom", is_error=True)

        assert r.output == "boom"
        assert r.is_error is True

    def test_extended_carries_arbitrary_dict(self) -> None:
        from matrix.model.chat import ToolCallResult

        r = ToolCallResult(
            output="text",
            extended={"content": [{"type": "image", "data": "..."}]},
        )

        assert r.extended == {"content": [{"type": "image", "data": "..."}]}
