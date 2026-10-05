"""``split_for_compaction``: the tail tier 2 keeps, made safe to summarise around."""

from __future__ import annotations

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tail import pending_from, split_for_compaction, unit_starts
from primer.model.chat import CompactionSummary, Message, TextPart, ToolCallPart, ToolResultPart

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
    def test_it_is_the_user_run_and_the_tool_rounds_after_it(self) -> None:
        messages = [_m("user"), _m("assistant"), _m("user"), _call(0), _result(0)]
        assert pending_from(messages) == 2, "the question and its in-flight tool round are both unanswered"

    def test_tool_rounds_of_an_earlier_turn_that_ended_without_a_reply_are_not_pending(self) -> None:
        """A Stop, the max_tool_turns cap or an empty completion leaves rounds with no final text reply. Once a later
        user message follows them they are history: treating them as pending left the whole session unsummarisable."""
        messages = [_m("user", "u1"), _call(0), _result(0), _call(1), _result(1), _m("user", "u2")]
        assert pending_from(messages) == 5, "only u2"
        split = _split(messages, turns=4)
        assert split.head and split.tail[-1].parts[0].text == "u2"

    def test_a_resumed_turn_keeps_its_question_and_its_round(self) -> None:
        messages = [_m("user", "old"), _m("assistant", "a"), _m("user", "q"), _call(0), _result(0)]
        assert pending_from(messages) == 2
        assert [t.role for t in _split(messages, turns=4, budget=0).tail] == ["user", "assistant", "tool"]

    def test_several_queued_user_messages_are_all_pending(self) -> None:
        messages = [_m("user", "old"), _m("assistant", "a"), _m("user", "q1"), _m("user", "q2")]
        assert pending_from(messages) == 2

    def test_rounds_after_a_summary_with_no_user_message_in_front_are_pending(self) -> None:
        assert pending_from([_m("assistant", "[summary]"), _call(0), _result(0)]) == 1

    def test_a_turn_with_no_answer_yet_is_entirely_pending(self) -> None:
        assert pending_from([_m("user"), _call(0), _result(0)]) == 0

    def test_a_history_ending_on_an_answer_has_nothing_pending(self) -> None:
        messages = [_m("user"), _m("assistant")]
        assert pending_from(messages) == len(messages)


def _summary(text: str = "[summary]") -> CompactionSummary:
    return CompactionSummary(role="assistant", parts=[TextPart(text=text)])


class TestACompactionSummaryIsTransparentToThePendingTurn:
    """After a compaction that summarised the early rounds of a turn the history is [question, summary, newest round].
    The summary is an assistant message but not an answer: the question is still the unanswered input."""

    @pytest.mark.parametrize(
        ("messages", "expected"),
        [
            ([_m("user", "q"), _summary(), _call(0), _result(0)], 0),
            ([_m("user", "q1"), _m("user", "q2"), _summary(), _call(0), _result(0)], 0),
            ([_m("user", "o"), _m("assistant", "a"), _m("user", "q"), _summary(), _call(0), _result(0)], 2),
            ([_m("user", "q"), _summary("s1"), _summary("s2"), _call(0), _result(0)], 0),
            ([_m("user", "q"), _summary(), _call(0), _result(0), _call(1), _result(1)], 0),
        ],
        ids=["one-question", "queued-questions", "after-an-earlier-turn", "two-summaries", "two-rounds"],
    )
    def test_the_walk_goes_through_the_summary_to_the_question(self, messages, expected) -> None:
        assert pending_from(messages) == expected

    def test_an_ordinary_assistant_reply_still_ends_the_pending_turn(self) -> None:
        """Only the compactor's own summary is transparent: a reply the model wrote answered the question."""
        assert pending_from([_m("user", "q"), _m("assistant", "[earlier conversation compacted on x]"), _call(0), _result(0)]) == 2

    def test_a_summary_with_no_question_in_front_of_it_is_not_pending(self) -> None:
        assert pending_from([_summary(), _call(0), _result(0)]) == 1
        assert pending_from([_summary(), _m("user", "q"), _call(0), _result(0)]) == 1

    def test_a_history_that_ends_on_the_summary_has_nothing_pending(self) -> None:
        messages = [_m("user", "q"), _summary()]
        assert pending_from(messages) == len(messages)

    def test_a_later_question_after_the_rounds_is_the_only_pending_input(self) -> None:
        assert pending_from([_m("user", "q"), _summary(), _call(0), _result(0), _m("user", "q2")]) == 4

    def test_a_second_split_keeps_the_question_and_folds_the_first_summary(self) -> None:
        history = [_m("user", "THE QUESTION"), _summary("S1"), *[m for i in range(6) for m in (_call(i), _result(i))]]
        split = _split(history, turns=4, budget=0)
        assert split.tail[0].parts[0].text == "THE QUESTION" and split.summary_after == 1
        assert split.head[0] is history[1], "the first summary is in what the second one replaces"
        assert [t.role for t in split.tail] == ["user", "assistant", "tool"], "the question and the newest round"
        assert split.summary_input[:2] == history[:2], "the summariser reads the question and the first summary"


class TestTheCostOfShrinkingATurn:
    def test_the_in_turn_shrink_does_not_measure_the_whole_tail_for_every_round(self) -> None:
        """500 rounds took about 3 seconds a call (the tail was measured again for every round replaced) and blocked
        the event loop. Counted in messages measured, not in seconds, so the test cannot flake."""
        measured = 0

        def counting(messages) -> int:
            nonlocal measured
            measured += len(messages)
            return SIZE(messages)

        history = [_m("user", "q"), *[m for i in range(500) for m in (_call(i), _result(i))]]
        split = split_for_compaction(history, tail_turns=4, tail_budget_tokens=1, size=counting)
        assert split.head and len(split.tail) == 3, "the shrink reached the floor"
        assert measured < 10 * len(history), f"{measured} messages measured for a history of {len(history)}"


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

    def test_a_history_that_fits_its_budget_is_still_cut_at_its_floor_so_there_is_something_to_summarise(self) -> None:
        messages = [_m("user", "q0"), _m("assistant", "a0"), _m("user", "q1"), _m("assistant", "a1"), _m("user", "Q")]
        split = _split(messages, turns=4)
        assert split.head == messages[:4] and split.tail == messages[4:] and split.reason is None

    def test_an_idle_history_keeps_its_newest_unit_at_the_floor(self) -> None:
        messages = [_m("user", "q0"), _m("assistant", "a0"), _m("user", "q1"), _m("assistant", "a1")]
        split = _split(messages, turns=4)
        assert split.tail == messages[3:] and len(split.head) == 3

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


def _turn(rounds: int, *, before: list[Message] | None = None, users: int = 1, size: int = 400) -> list[Message]:
    """``before`` history, then ``users`` opening user messages, then ``rounds`` tool rounds of ``size`` chars each."""
    out = list(before or [])
    out += [_m("user", f"U{i}") for i in range(users)]
    for i in range(rounds):
        out += [_call(i), Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output="r" * size)])]
    return out


def _answered(messages) -> bool:
    calls = {p.id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    results = {p.id for m in messages for p in m.parts if isinstance(p, ToolResultPart)}
    return calls == results


class TestInsideTheCurrentTurn:
    """The opening user run and the NEWEST round are protected; the rounds between them can be summarised."""

    def test_the_floor_is_the_user_run_and_the_newest_round(self) -> None:
        history = _turn(6, before=[_m("user", "old"), _m("assistant", "old reply")])
        split = _split(history, turns=4, budget=0)
        assert [t.parts[0].text if isinstance(t.parts[0], TextPart) else t.role for t in split.tail] == ["U0", "assistant", "tool"]
        assert split.tail[1].parts[0].id == "c5", "the newest round"
        assert split.summary_after == 1, "the summary goes after the one user message"

    def test_the_head_is_what_came_before_the_turn_plus_its_early_rounds_in_order(self) -> None:
        history = _turn(4, before=[_m("user", "old"), _m("assistant", "old reply")])
        split = _split(history, turns=4, budget=0)
        assert split.head == [*history[:2], *history[3:9]], "the two old messages, then rounds 0-2 (U0 stays)"
        assert split.reason is None

    def test_the_summariser_reads_the_question_with_the_rounds_it_summarises(self) -> None:
        history = _turn(4, before=[_m("user", "old"), _m("assistant", "old reply")])
        split = _split(history, turns=4, budget=0)
        assert split.summary_input == history[:9], "everything up to the last replaced round, the user run included"

    def test_rounds_are_removed_oldest_first_only_as_far_as_the_budget_needs(self) -> None:
        history = _turn(10, size=4_000)  # about 1,000 tokens a round
        split = _split(history, turns=4, budget=3_500)
        kept_ids = [p.id for m in split.tail for p in m.parts if isinstance(p, ToolCallPart)]
        assert kept_ids == [f"c{i}" for i in range(10 - len(kept_ids), 10)], "the newest rounds, contiguous"
        assert 2 <= len(kept_ids) <= 4 and SIZE(split.tail) <= 3_500

    def test_a_single_runaway_turn_with_nothing_before_it_can_still_be_shrunk(self) -> None:
        """[U, R1..Rk] used to be entirely protected: an empty head, unreducible, however big it grew."""
        split = _split(_turn(8), turns=4, budget=0)
        assert split.head and split.reason is None and len(split.tail) == 3

    def test_a_turn_within_the_budget_is_left_whole_and_the_summary_goes_in_front(self) -> None:
        history = _turn(3, before=[_m("user", "old"), _m("assistant", "old reply")])
        split = _split(history, turns=1, budget=10**9)
        assert split.summary_after == 0 and split.head == history[:2] and len(split.tail) == 7

    def test_every_queued_user_message_of_the_turn_stays_verbatim_before_the_summary(self) -> None:
        split = _split(_turn(5, users=2), turns=4, budget=0)
        assert [t.parts[0].text for t in split.tail[:2]] == ["U0", "U1"] and split.summary_after == 2

    def test_a_turn_without_a_user_run_puts_the_summary_in_front(self) -> None:
        """After a compaction a resumed turn can start with a summary and no user message of its own."""
        history = [_m("assistant", "[summary]"), *_turn(4, users=0)[0:]]
        split = _split(history, turns=4, budget=0)
        assert split.summary_after == 0 and len(split.tail) == 2

    @pytest.mark.parametrize("budget", [0, 600, 1_500, 3_000, 10**6])
    @pytest.mark.parametrize("turns", [0, 2, 4])
    def test_every_cut_is_provider_valid_with_the_summary_in_place(self, budget: int, turns: int) -> None:
        history = _turn(9, before=[_m("user", "o1"), _m("assistant", "a1"), _m("user", "o2"), _m("assistant", "a2")], size=2_000)
        split = _split(history, turns=turns, budget=budget)
        assembled = [*split.tail[: split.summary_after], _m("assistant", "SUMMARY"), *split.tail[split.summary_after:]]
        assert _answered(split.tail) and _answered(split.head) and _answered(assembled)
        assert split.tail[: split.summary_after] == [t for t in split.tail if t.role == "user"][: split.summary_after]
        assert sorted(map(id, split.head + split.tail)) == sorted(map(id, history)), "a partition of the history"
