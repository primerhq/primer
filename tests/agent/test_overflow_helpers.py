"""The pieces of overflow recovery that do no I/O: the replay guard, the summariser's reduced copy, the round helpers."""

from __future__ import annotations

import asyncio

from primer.agent.compaction import CompactionStrategy
from primer.agent.overflow import ReplayGuard, reduce_head_for_summary, tool_rounds, whole_rounds
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart

SIZE = CompactionStrategy._estimate_tokens  # noqa: SLF001


def _call(i: int) -> Message:
    return Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="exec", arguments={})])


def _result(i: int, chars: int) -> Message:
    return Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output=chr(97 + i) * chars)])


def _user(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def _outputs(messages) -> list[str]:
    return [p.output for m in messages for p in m.parts if isinstance(p, ToolResultPart)]


def _before(guard: ReplayGuard, prompt):
    return asyncio.run(guard.before_call(prompt, tools=[]))


class TestReplayGuard:
    def test_a_prompt_under_the_target_goes_out_as_it_is(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 4_000)]
        guard = ReplayGuard(target_tokens=50_000, size=SIZE)
        assert _before(guard, prompt) == prompt and guard.shed_calls == 0

    def test_a_prompt_over_the_target_has_its_tool_results_reduced_and_the_input_is_not_modified(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000)]
        raw = _outputs(prompt)
        sent = _before(ReplayGuard(target_tokens=30_000, size=SIZE), prompt)
        assert SIZE(sent) <= 30_000
        assert _outputs(prompt) == raw, "the caller's own messages are never touched"
        assert [m.role for m in sent] == [m.role for m in prompt], "the envelope stays: no tool call loses its result"

    def test_the_newest_round_is_reduced_too_when_it_is_the_reason_for_the_overflow(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 600_000)]
        sent = _before(ReplayGuard(target_tokens=30_000, size=SIZE), prompt)
        assert len(_outputs(sent)[0]) < 20_000

    def test_what_was_pruned_stays_pruned_on_the_next_call_of_the_same_turn(self) -> None:
        guard = ReplayGuard(target_tokens=30_000, size=SIZE)
        first = [_user("go"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000)]
        sent_first = _before(guard, first)
        second = [*first, _call(2), _result(2, 100)]
        sent_second = _before(guard, second)
        assert _outputs(sent_second)[:2] == _outputs(sent_first), "the same results are reduced the same way"
        assert _outputs(sent_second)[2] == "c" * 100

    def test_a_prompt_with_nothing_to_shed_is_sent_unchanged_not_failed(self) -> None:
        prompt = [_user("u" * 400_000)]
        assert _before(ReplayGuard(target_tokens=1_000, size=SIZE), prompt) == prompt


class TestReduceHeadForSummary:
    def test_tool_results_are_pruned_first_and_the_envelope_and_order_survive(self) -> None:
        head = [_user("q"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000), _user("more")]
        reduced = reduce_head_for_summary(head, target_tokens=20_000, size=SIZE)
        assert SIZE(reduced) <= 20_000
        assert [m.role for m in reduced] == [m.role for m in head]
        assert [p.id for m in reduced for p in m.parts if isinstance(p, ToolCallPart)] == ["c0", "c1"]

    def test_long_texts_are_cut_to_an_even_share_with_the_cut_marked_and_short_ones_kept(self) -> None:
        head = [_user("short"), _user("L" * 400_000), _user("M" * 400_000)]
        reduced = reduce_head_for_summary(head, target_tokens=20_000, size=SIZE)
        texts = [p.text for m in reduced for p in m.parts]
        assert texts[0] == "short"
        assert all("characters omitted before summarising" in t and len(t) < 40_000 for t in texts[1:])
        assert texts[1].startswith("L") and texts[1].endswith("L")

    def test_a_head_already_under_the_target_comes_back_equal(self) -> None:
        head = [_user("q"), _call(0), _result(0, 400)]
        assert reduce_head_for_summary(head, target_tokens=50_000, size=SIZE) == head

    def test_the_input_is_not_modified(self) -> None:
        head = [_user("L" * 400_000)]
        reduce_head_for_summary(head, target_tokens=1_000, size=SIZE)
        assert head[0].parts[0].text == "L" * 400_000


class TestRoundHelpers:
    def test_whole_rounds_drops_only_a_trailing_tool_call_nothing_answered(self) -> None:
        answered = [_user("q"), _call(0), _result(0, 10)]
        assert whole_rounds(answered) == answered
        assert whole_rounds([*answered, _call(1)]) == answered
        assert whole_rounds([]) == []

    def test_tool_rounds_counts_assistant_messages_with_a_tool_call(self) -> None:
        text_only = Message(role="assistant", parts=[TextPart(text="hi")])
        assert tool_rounds([_user("q"), _call(0), _result(0, 1), text_only, _call(1), _result(1, 1)]) == 2
