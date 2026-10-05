"""``[assistant tool_use, tool result, user]`` is what a failed turn leaves in the history and a new user message follows.

The overflow recovery persists the completed tool rounds of a turn that ended in failure (so the next turn does
not run them again), and the user's next message then lands right after them. The adapters must turn that
shape into a request the provider accepts: the tool_use answered by the very next message, the result carrying
the call's id (and, for Gemini, its name), and the user's text after it, not between them.
"""

from __future__ import annotations

from primer.llm.anthropic import _messages_to_anthropic
from primer.llm.gemini import _messages_to_gemini
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart


def _history() -> list[Message]:
    return [
        Message(role="user", parts=[TextPart(text="read the files")]),
        Message(role="assistant", parts=[ToolCallPart(id="call_1", name="workspace__read", arguments={"path": "a.txt"})]),
        Message(role="tool", parts=[ToolResultPart(id="call_1", output="[... 5000 chars omitted; this call ALREADY RAN, do NOT call it again ...]")]),
        Message(role="user", parts=[TextPart(text="try again, but smaller")]),
    ]


def test_anthropic_answers_the_tool_use_in_the_next_message_and_puts_the_user_text_after_it() -> None:
    _, messages = _messages_to_anthropic(_history())
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "user"]
    assert messages[1]["content"][0]["type"] == "tool_use" and messages[1]["content"][0]["id"] == "call_1"
    answer = messages[2]["content"]
    assert answer[0]["type"] == "tool_result" and answer[0]["tool_use_id"] == "call_1", "the very next message answers it"
    assert messages[3]["content"][0]["type"] == "text", "the user's text comes after the result, not between call and result"


def test_gemini_answers_the_function_call_in_the_next_content_with_the_calls_name_and_id() -> None:
    _, contents = _messages_to_gemini(_history())
    assert [c.role for c in contents] == ["user", "model", "user", "user"]
    call = contents[1].parts[0].function_call
    response = contents[2].parts[0].function_response
    assert call.name == "workspace__read"
    assert response is not None and response.name == "workspace__read", "the result names the call it answers"
    assert response.id == "call_1" == call.id
    assert contents[3].parts[0].text == "try again, but smaller"
