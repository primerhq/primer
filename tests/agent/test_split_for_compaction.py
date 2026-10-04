"""``split_for_compaction``: the tail tier 2 keeps, made safe to summarise around."""

from __future__ import annotations

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tail import pending_from, split_for_compaction, unit_starts
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart

SIZE = CompactionStrategy._estimate_tokens  # noqa: SLF001


def _m(role: str, text: str = "x") -> Message:
    return Message(role=role, parts=[TextPart(text=text)])


def _call(i: int) -> Message:
    return Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="exec", arguments={})])


def _result(i: int, *more: int) -> Message:
    return Message(role="tool", parts=[ToolResultPart(id=f"c{j}", output="r") for j in (i, *more)])


def _split(messages, *, turns=4, budget=10**9):
    return split_for_compaction(messages, tail_turns=turns, tail_budget_tokens=budget, size=SIZE)


class TestUnits:
    def test_a_tool_call_and_all_its_results_are_one_unit(self) -> None:
        messages = [_m("user"), _call(0), _result(0), _m("assistant"), _call(1), _result(1), _m("user")]
        assert unit_starts(messages) == [0, 1, 3, 4, 6]

    def test_parallel_results_stay_with_their_call_across_several_tool_messages(self) -> None:
        messages = [_call(0), _result(0), _result(1), _m("assistant")]
        assert unit_starts(messages) == [0, 3]


class TestPending:
    def test_it_starts_after_the_last_assistant_message_that_made_no_tool_call(self) -> None:
        messages = [_m("user"), _m("assistant"), _m("user"), _call(0), _result(0)]
        assert pending_from(messages) == 2, "the question and its in-flight tool round are both unanswered"

    def test_with_no_final_answer_everything_is_pending(self) -> None:
        assert pending_from([_m("user"), _call(0), _result(0)]) == 0

    def test_a_history_ending_on_an_answer_has_nothing_pending(self) -> None:
        messages = [_m("user"), _m("assistant")]
        assert pending_from(messages) == len(messages)


class TestTheCut:
    def test_the_turn_based_tail_is_what_tail_split_gives_when_it_is_within_the_budget(self) -> None:
        messages = [m for i in range(6) for m in (_m("user", f"q{i}"), _m("assistant", f"a{i}"))]
        split = _split(messages, turns=2)
        assert [t.parts[0].text for t in split.tail] == ["a4", "q5", "a5"]
        assert len(split.head) == 9 and split.reason is None

    def test_the_pending_input_is_in_the_tail_even_when_the_turn_boundary_is_after_it(self) -> None:
        messages = [_m("user", "old"), _m("assistant", "a"), _m("user", "QUESTION")]
        split = _split(messages, turns=0)
        assert [t.parts[0].text for t in split.tail] == ["QUESTION"]

    def test_the_budget_moves_the_oldest_units_into_the_head_but_never_the_pending_suffix(self) -> None:
        big = "x" * 4_000
        messages = [_m("user", big), _m("assistant", big), _m("user", big), _m("assistant", big), _m("user", "Q" * 8_000)]
        split = _split(messages, turns=4, budget=1)
        assert split.tail == messages[4:], "shrunk to the pending input, which alone exceeds the budget"
        assert split.head == messages[:4]

    def test_fewer_assistants_than_tail_turns_no_longer_leaves_an_empty_head_when_over_budget(self) -> None:
        big = "x" * 4_000
        messages = [_m("user", big), _m("assistant", big), _m("user", big), _m("assistant", big), _m("user", "Q")]
        split = _split(messages, turns=4, budget=1_500)
        assert split.head and split.reason is None

    def test_an_empty_head_is_named(self) -> None:
        split = _split([_m("user", "only input")])
        assert split.head == [] and split.reason == "empty_head"

    def test_a_shrink_never_summarises_the_final_unit(self) -> None:
        big = "x" * 4_000
        messages = [_m("user", big), _m("assistant", big)]
        split = _split(messages, turns=4, budget=1)
        assert split.tail == messages[1:], "the newest unit stays even when it alone is over the budget"

    def test_negative_tail_turns_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            _split([_m("user")], turns=-1)

    @pytest.mark.parametrize("budget", [0, 5, 20, 80, 10**6])
    @pytest.mark.parametrize("turns", [0, 1, 2, 4])
    def test_head_and_tail_are_a_partition_cut_on_a_unit_boundary(self, budget: int, turns: int) -> None:
        messages = [_m("user", "q0"), _call(0), _result(0, 1), _call(2), _result(2), _m("assistant", "a"), _m("user", "q1"), _call(3), _result(3)]
        split = _split(messages, turns=turns, budget=budget)
        assert split.head + split.tail == messages
        assert len(split.head) in {0, *unit_starts(messages)}
        assert len(split.head) <= pending_from(messages) or len(split.head) == len(messages)
