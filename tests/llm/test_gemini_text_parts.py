"""Gemini streamed text is one part, not one part per chunk (Part 2 T51, AG-L8).

Every streamed text chunk took a new index, so the assistant message held one ``TextPart`` per chunk (history bloat) and
the Ollama wire joined them with newlines after a failover.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from primer.llm.gemini import _StreamState, _translate_chunk
from primer.llm.ollama import _messages_to_ollama
from primer.model.chat import Message, ReasoningDelta, TextDelta, TextPart, ToolCallStart, output_to_message


def _chunk(*parts) -> NS:
    return NS(response_id="r", candidates=[NS(content=NS(parts=list(parts)), finish_reason=None, grounding_metadata=None,
                                              safety_ratings=None, logprobs_result=None)], usage_metadata=None)


def _text(text: str, thought=None) -> NS:
    return NS(text=text, thought=thought)


def _call(name: str) -> NS:
    return NS(text=None, function_call=NS(id=None, name=name, args={}))


def _events(*chunks) -> list:
    state = _StreamState()
    return [e for c in chunks for e in _translate_chunk(c, state, "gemini-x")]


def test_three_text_chunks_are_one_part() -> None:
    msg = output_to_message(_events(_chunk(_text("Hel")), _chunk(_text("lo wor")), _chunk(_text("ld"))))
    assert msg.parts == [TextPart(text="Hello world")]
    wire = _messages_to_ollama([Message(role="user", parts=[TextPart(text="hi")]), msg])
    assert wire[-1]["content"] == "Hello world"


def test_a_part_of_another_kind_starts_a_new_index() -> None:
    events = _events(
        _chunk(_text("think", thought=True)), _chunk(_text("ing", thought=True)),
        _chunk(_text("Let me")), _chunk(_text(" look")),
        _chunk(_call("read_file")),
        _chunk(_text("Done")),
    )
    indexed = [(type(e).__name__, e.index) for e in events if isinstance(e, (TextDelta, ReasoningDelta, ToolCallStart))]
    assert indexed == [
        ("ReasoningDelta", 0), ("ReasoningDelta", 0),
        ("TextDelta", 1), ("TextDelta", 1),
        ("ToolCallStart", 2),
        ("TextDelta", 3),
    ]
