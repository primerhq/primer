"""The pieces of overflow recovery that do no I/O: completed rounds, the replay guard, the reduced form."""

from __future__ import annotations

import asyncio

from primer.agent.compaction import CompactionStrategy
from primer.agent.overflow import ReplayGuard, completed_rounds, reduce_for_persist, tool_rounds
from primer.agent.prune import ALREADY_RAN_PLACEHOLDERS, PruneSet, prune_prompt
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


class TestCompletedRounds:
    def test_a_trailing_assistant_message_is_not_a_completed_round(self) -> None:
        rounds = [_call(0), _result(0, 5), _call(1)]
        assert completed_rounds(rounds) == rounds[:2], "a call whose dispatch never finished goes"

    def test_a_trailing_text_reply_that_was_streaming_goes_too(self) -> None:
        rounds = [_call(0), _result(0, 5), Message(role="assistant", parts=[TextPart(text="half a reply")])]
        assert completed_rounds(rounds) == rounds[:2]

    def test_nothing_completed_is_nothing(self) -> None:
        assert completed_rounds([]) == [] and completed_rounds([_call(0)]) == []

    def test_whole_rounds_are_kept_as_they_are(self) -> None:
        rounds = [_call(0), _result(0, 5), _call(1), _result(1, 5)]
        assert completed_rounds(rounds) == rounds

    def test_tool_rounds_counts_assistant_messages_that_carry_a_call(self) -> None:
        text_only = Message(role="assistant", parts=[TextPart(text="hi")])
        assert tool_rounds([_user("q"), _call(0), _result(0, 1), text_only, _call(1), _result(1, 1)]) == 2


class TestAlreadyRanPlaceholders:
    def test_the_default_placeholder_tells_the_model_to_call_again_and_the_replay_one_says_not_to(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000), _call(2), _result(2, 100)]
        default = prune_prompt(prompt, shed_tokens=10_000).messages
        ran = prune_prompt(prompt, shed_tokens=10_000, placeholders=ALREADY_RAN_PLACEHOLDERS).messages
        assert any("call the tool again" in o for o in _outputs(default))
        assert not any("call the tool again" in o for o in _outputs(ran))
        assert any("ALREADY RAN" in o and "do NOT call it again" in o for o in _outputs(ran))

    def test_a_forced_cut_uses_the_replay_text_too(self) -> None:
        prompt = [_call(0), _result(0, 400_000)]
        cut = prune_prompt(prompt, shed_tokens=50_000, force=True, keep_rounds=0, placeholders=ALREADY_RAN_PLACEHOLDERS).messages
        assert "ALREADY RAN" in _outputs(cut)[0] and len(_outputs(cut)[0]) < 20_000


class TestReplayGuard:
    @staticmethod
    def _before(guard: ReplayGuard, prompt):
        return asyncio.run(guard.before_call(prompt, tools=[]))

    def test_a_prompt_under_the_target_goes_out_as_it_is(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 4_000)]
        guard = ReplayGuard(target_tokens=50_000, size=SIZE)
        assert self._before(guard, prompt) == prompt and guard.shed_calls == 0

    def test_a_prompt_over_the_target_has_its_tool_results_reduced_and_the_input_is_not_modified(self) -> None:
        prompt = [_user("go"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000)]
        raw = _outputs(prompt)
        sent = self._before(ReplayGuard(target_tokens=30_000, size=SIZE), prompt)
        assert SIZE(sent) <= 30_000 and _outputs(prompt) == raw
        assert [m.role for m in sent] == [m.role for m in prompt], "the envelope stays"
        assert any("ALREADY RAN" in o for o in _outputs(sent))

    def test_what_was_pruned_stays_pruned_on_the_next_call_of_the_same_replay(self) -> None:
        guard = ReplayGuard(target_tokens=30_000, size=SIZE)
        first = [_user("go"), _call(0), _result(0, 200_000), _call(1), _result(1, 200_000)]
        sent_first = self._before(guard, first)
        sent_second = self._before(guard, [*first, _call(2), _result(2, 100)])
        assert _outputs(sent_second)[:2] == _outputs(sent_first) and _outputs(sent_second)[2] == "c" * 100
        assert guard.prune_set, "and the guard can say what it reduced"

    def test_a_prompt_with_nothing_to_shed_is_sent_unchanged_not_failed(self) -> None:
        prompt = [_user("u" * 400_000)]
        assert self._before(ReplayGuard(target_tokens=1_000, size=SIZE), prompt) == prompt


class TestReduceForPersist:
    def test_the_form_the_model_last_saw_is_what_is_persisted(self) -> None:
        rounds = [_call(0), _result(0, 200_000), _call(1), _result(1, 100)]
        guard = ReplayGuard(target_tokens=10_000, size=SIZE)
        asyncio.run(guard.before_call([_user("go"), *rounds], tools=[]))
        reduced = reduce_for_persist(rounds, sticky=guard.prune_set, target_tokens=10**9, size=SIZE)
        assert "ALREADY RAN" in _outputs(reduced)[0] and _outputs(reduced)[1] == "b" * 100

    def test_results_are_cut_further_when_the_rounds_are_still_over_the_target(self) -> None:
        rounds = [_call(0), _result(0, 400_000)]
        reduced = reduce_for_persist(rounds, sticky=PruneSet(), target_tokens=5_000, size=SIZE)
        assert len(_outputs(reduced)[0]) < 20_000 and "ALREADY RAN" in _outputs(reduced)[0]
        assert [m.role for m in reduced] == ["assistant", "tool"], "the call keeps its result"

    def test_rounds_under_the_target_are_left_as_they_are(self) -> None:
        rounds = [_call(0), _result(0, 400)]
        assert reduce_for_persist(rounds, sticky=PruneSet(), target_tokens=50_000, size=SIZE) == rounds

    def test_the_input_is_not_modified(self) -> None:
        rounds = [_call(0), _result(0, 400_000)]
        reduce_for_persist(rounds, sticky=PruneSet(), target_tokens=5_000, size=SIZE)
        assert _outputs(rounds) == ["a" * 400_000]
