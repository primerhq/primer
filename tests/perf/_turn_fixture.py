"""A fixed history and tool catalogue for the per-turn CPU tests (T9): the same objects every call, as the rounds of one turn see them."""

from __future__ import annotations

from primer.model.chat import Message, TextPart, Tool, ToolCallPart, ToolResultPart


def history(n: int = 200) -> list[Message]:
    out: list[Message] = []
    for i in range(n // 4):
        out.append(Message(role="user", parts=[TextPart(text=f"question {i} " + "x" * (i % 17))]))
        out.append(Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="search", arguments={"q": f"term {i}", "n": i, "deep": {"a": [1, 2, 3]}})]))
        out.append(Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output=f"result {i} " * (3 + i % 11))]))
        out.append(Message(role="assistant", parts=[TextPart(text="answer " * (1 + i % 5))]))
    return out


def tools(n: int = 60) -> list[Tool]:
    return [
        Tool(
            id=f"toolset__tool_{i}", toolset_id="toolset", description=f"does thing {i} " * (1 + i % 4),
            args_schema={"type": "object", "properties": {f"p{j}": {"type": "string", "description": "d" * j} for j in range(1 + i % 6)}},
        )
        for i in range(n)
    ]
