"""Unit tests for the char-heuristic token counter."""

from __future__ import annotations

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    ImagePart,
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)


class TestCountTokensCharFallback:
    def test_text_only(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello world")])]
        # 8 overhead + ceil(11/4)=3 = 11
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 11

    def test_multi_message(self) -> None:
        msgs = [
            Message(role="user", parts=[TextPart(text="abcd")]),
            Message(role="assistant", parts=[TextPart(text="abcdefgh")]),
        ]
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 19

    def test_tool_call_part(self) -> None:
        msgs = [
            Message(
                role="assistant",
                parts=[ToolCallPart(id="c1", name="ls", arguments={"path": "/"})],
            )
        ]
        # 8 + (50 + 2 + ceil(13/4)=4) = 64
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 64

    def test_tool_result_part(self) -> None:
        msgs = [
            Message(
                role="tool",
                parts=[ToolResultPart(id="c1", output="ok", error=False)],
            )
        ]
        # 8 + (20 + 1) = 29
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 29

    def test_image_part(self) -> None:
        msgs = [
            Message(
                role="user",
                parts=[ImagePart(mime_type="image/png", data=b"\x00\x01")],
            )
        ]
        # 8 + 1000 = 1008
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 1008

    def test_tools_serialised_into_total(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        tools = [
            Tool(
                id="ls",
                description="list dir",
                toolset_id="x",
                args_schema={"type": "object", "properties": {}},
            )
        ]
        result = count_tokens_char_fallback(messages=msgs, tools=tools)
        assert result > 9
