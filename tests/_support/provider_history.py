"""Assertions that a persisted history is one a provider will accept.

Anthropic requires a ``tool_result`` for every ``tool_use`` in the very next user message; OpenAI Chat
Completions requires a tool message after every assistant ``tool_calls``. A history that ends a round
with an unanswered call is a 400 on every later request, so a turn that stops without running a round's
tool calls (a Stop, the tool-turn cap) must still answer them. These run the REAL provider translations
over a message list, so a regression in either the loop or a translator shows up here.
"""

from __future__ import annotations

from primer.llm._openai_compat import _messages_to_chat
from primer.llm.anthropic import _messages_to_anthropic
from primer.model.chat import Message


def assert_anthropic_valid(messages: list[Message]) -> None:
    _, rows = _messages_to_anthropic(messages)
    pending: set[str] = set()
    for row in rows:
        blocks = row["content"] if isinstance(row["content"], list) else []
        if row["role"] == "assistant":
            assert not pending, f"tool_use ids with no tool_result before the next assistant turn: {pending}"
            pending = {b["id"] for b in blocks if b.get("type") == "tool_use"}
        elif pending:
            answered = {b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"}
            assert answered >= pending, f"tool_use ids not answered by the next user turn: {pending - answered}"
            pending = set()
    assert not pending, f"the history ends with unanswered tool_use ids: {pending}"


def assert_openai_valid(messages: list[Message]) -> None:
    pending: set[str] = set()
    for row in _messages_to_chat(messages):
        if row["role"] == "tool":
            pending.discard(row["tool_call_id"])
            continue
        assert not pending, f"assistant tool_calls not followed by tool messages: {pending}"
        if row["role"] == "assistant":
            pending = {c["id"] for c in row.get("tool_calls") or []}
    assert not pending, f"the history ends with unanswered tool_calls: {pending}"
