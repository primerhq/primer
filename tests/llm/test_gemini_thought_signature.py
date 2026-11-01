"""Gemini thought signatures survive a tool round (Part 2 T51, AG-M21).

Google documents the ``thought_signature`` on a Gemini 3 function call as mandatory on the next request of the tool round
(the API can answer 400 without it). The adapter dropped it on read and sent the ``FunctionCall`` back without one. It now
rides ``ToolCallEnd`` into an optional ``ToolCallPart.thought_signature`` (base64 text) and is sent back on the call.
"""

from __future__ import annotations

import json

import primer.llm.gemini as gemini_mod
from primer.model.chat import Message, TextPart, ToolCallEnd, ToolCallPart, ToolResultPart, output_to_message
from tests.llm.test_adapter_characterization import _OK, _provider, _route

SIGNATURE = "c2lnLTEyMw=="  # base64 of b"sig-123"


def _sse(*payloads: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(p).encode() + b"\r\n\r\n" for p in payloads)


CALL = _sse({"candidates": [{"content": {"role": "model", "parts": [
    {"functionCall": {"id": "fc1", "name": "read_file", "args": {"path": "a.txt"}}, "thoughtSignature": SIGNATURE},
]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}, "responseId": "r1"})
ANSWER = _sse({"candidates": [{"content": {"role": "model", "parts": [{"text": "It says hi."}]}, "finishReason": "STOP"}],
               "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}, "responseId": "r2"})


class TestWithoutASignatureNothingChanges:
    """Characterization: a call that carries no signature serialises exactly as before."""

    def test_tool_call_part(self) -> None:
        part = ToolCallPart(id="c", name="n", arguments={"a": 1})
        assert part.model_dump() == {"type": "tool_call", "id": "c", "name": "n", "arguments": {"a": 1}}
        assert json.loads(part.model_dump_json()) == {"type": "tool_call", "id": "c", "name": "n", "arguments": {"a": 1}}

    def test_tool_call_end(self) -> None:
        end = ToolCallEnd(id="c", arguments={}, index=0)
        assert end.model_dump() == {"type": "tool_call_end", "id": "c", "arguments": {}, "index": 0}

    def test_the_gemini_wire(self) -> None:
        _, contents = gemini_mod._messages_to_gemini([
            Message(role="user", parts=[TextPart(text="hi")]),
            Message(role="assistant", parts=[ToolCallPart(id="fc1", name="read_file", arguments={"path": "a.txt"})]),
        ])
        assert contents[1].parts[0].thought_signature is None


async def test_a_tool_round_sends_the_signature_back(monkeypatch) -> None:
    wire = _route(monkeypatch, "gemini", [(200, CALL, _OK), (200, ANSWER, _OK)])
    llm = gemini_mod.GeminiLLM(_provider("gemini"))
    try:
        history = [Message(role="user", parts=[TextPart(text="read a.txt")])]
        first = [e async for e in llm.stream(model="gemini-x", messages=history)]
        call = output_to_message(first)
        assert call.parts == [ToolCallPart(id="fc1", name="read_file", arguments={"path": "a.txt"}, thought_signature=SIGNATURE)]
        history += [call, Message(role="tool", parts=[ToolResultPart(id="fc1", output="hi")])]
        async for _ in llm.stream(model="gemini-x", messages=history):
            pass
    finally:
        await llm.aclose()
    sent = json.loads(wire.requests[1].content)["contents"][1]
    assert sent == {"role": "model", "parts": [
        {"functionCall": {"id": "fc1", "name": "read_file", "args": {"path": "a.txt"}}, "thoughtSignature": SIGNATURE},
    ]}


def test_a_signed_call_round_trips_through_the_message_log() -> None:
    part = ToolCallPart.model_validate_json(
        ToolCallPart(id="c", name="n", arguments={}, thought_signature=SIGNATURE).model_dump_json(),
    )
    assert part.thought_signature == SIGNATURE
