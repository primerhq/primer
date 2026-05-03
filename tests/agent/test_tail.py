"""Tests for matrix.agent.tail.tail_split."""

from __future__ import annotations

import pytest

from matrix.agent.tail import tail_split
from matrix.model.chat import Message, TextPart


def _u(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _a(text: str) -> Message:
    return Message(role="assistant", parts=[TextPart(text=text)])


def _t(text: str) -> Message:
    return Message(role="tool", parts=[TextPart(text=text)])


class TestTailSplit:
    def test_empty_input(self) -> None:
        head, tail = tail_split([], tail_turns=4)
        assert head == []
        assert tail == []

    def test_no_assistant_messages_keeps_all_in_head(self) -> None:
        msgs = [_u("a"), _u("b")]
        head, tail = tail_split(msgs, tail_turns=4)
        assert tail == []
        assert head == msgs

    def test_tail_turns_zero(self) -> None:
        msgs = [_u("a"), _a("b"), _u("c"), _a("d")]
        head, tail = tail_split(msgs, tail_turns=0)
        assert tail == []
        assert head == msgs

    def test_tail_turns_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            tail_split([], tail_turns=-1)

    def test_tail_turns_one(self) -> None:
        msgs = [_u("u0"), _a("a1"), _u("u2"), _a("a3"), _u("u4")]
        head, tail = tail_split(msgs, tail_turns=1)
        assert head == [_u("u0"), _a("a1"), _u("u2")]
        assert tail == [_a("a3"), _u("u4")]

    def test_tail_turns_two(self) -> None:
        msgs = [
            _u("u0"),
            _a("a1"),
            _t("t2"),
            _a("a3"),
            _u("u4"),
            _a("a5"),
            _t("t6"),
            _a("a7"),
        ]
        head, tail = tail_split(msgs, tail_turns=2)
        assert head == [_u("u0"), _a("a1"), _t("t2"), _a("a3"), _u("u4")]
        assert tail == [_a("a5"), _t("t6"), _a("a7")]

    def test_tail_turns_four(self) -> None:
        msgs = [
            _u("u0"),
            _a("a1"),
            _t("t2"),
            _a("a3"),
            _u("u4"),
            _a("a5"),
            _t("t6"),
            _a("a7"),
        ]
        head, tail = tail_split(msgs, tail_turns=4)
        assert head == [_u("u0")]
        assert tail == [
            _a("a1"),
            _t("t2"),
            _a("a3"),
            _u("u4"),
            _a("a5"),
            _t("t6"),
            _a("a7"),
        ]

    def test_tail_turns_exceeds_assistant_count_returns_empty_head(self) -> None:
        msgs = [_u("u0"), _a("a1"), _u("u2"), _a("a3")]
        head, tail = tail_split(msgs, tail_turns=10)
        assert head == []
        assert tail == msgs

    def test_returns_lists_not_input_sequence(self) -> None:
        msgs_tuple = (_u("u0"), _a("a1"))
        head, tail = tail_split(msgs_tuple, tail_turns=0)
        assert isinstance(head, list)
        assert isinstance(tail, list)
