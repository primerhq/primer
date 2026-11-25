"""The shared history split: the pending tail a runtime step must answer."""

from __future__ import annotations

import pytest

from primer.llm.agent_runtime.history import split_tail
from primer.llm.claude_code.history import plan_replay
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.except_ import BadRequestError


def _msg(role: str, *parts) -> Message:
    return Message(role=role, parts=list(parts))


def test_split_tail_user_and_tool_round():
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={}))
    t1 = _msg("tool", ToolResultPart(id="c1", output="contents"))
    u2 = _msg("user", TextPart(text="U2"))
    older, tail = split_tail([_msg("system", TextPart(text="S")), u1, a1, t1, u2])
    assert tail == [u2]
    assert older == [u1, a1, t1]
    # a history ending in tool results: the pending round IS the tail
    older, tail = split_tail([u1, a1, t1])
    assert tail == [a1, t1]
    assert older == [u1]


def test_split_tail_refuses_assistant_tail():
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", TextPart(text="A1"))
    with pytest.raises(BadRequestError, match="history must end with a user or tool message"):
        split_tail([u1, a1])
    with pytest.raises(BadRequestError):
        split_tail([_msg("system", TextPart(text="S"))])


def test_claude_replay_planner_leaves_terminal_validation_to_the_adapter():
    system = _msg("system", TextPart(text="instructions"))
    assistant = _msg("assistant", TextPart(text="last reply"))

    assert plan_replay([], name_of=str).tail == []
    system_only = plan_replay([system], name_of=str)
    assert (system_only.system_text, system_only.tail) == ("instructions", [])
    assistant_last = plan_replay([system, assistant], name_of=str)
    assert (assistant_last.system_text, assistant_last.tail) == ("instructions", [assistant])
