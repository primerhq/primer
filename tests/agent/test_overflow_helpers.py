"""The pieces of overflow recovery that do no I/O: completed rounds, the replay guard, the reduced form."""

from __future__ import annotations

import asyncio

from primer.agent.compaction import CompactionStrategy
from primer.agent.overflow import (
    ReplayGuard, cap_newest_round, completed_rounds, kept_rounds, reduce_for_persist, tool_rounds,
)
from primer.agent.prune import ALREADY_RAN_PLACEHOLDERS, PruneSet, prune_prompt, result_keys
from primer.model.chat import Message, TextPart, Tool, ToolCallPart, ToolResultPart
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

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

    @staticmethod
    def _schemas(chars: int) -> list[Tool]:
        return [Tool(
            id="t", description="d" * chars, toolset_id="ts",
            args_schema={"type": "object", "properties": {"a": {"type": "string", "description": "x" * chars}}},
        )]

    def test_the_tool_schemas_count_toward_the_target(self) -> None:
        """The messages alone are under the target; with the schemas that go out on every call they are over it.
        Counting only the messages reduced to a target the schemas then overflowed on their own."""
        prompt = [_user("go"), _call(0), _result(0, 60_000), _call(1), _result(1, 400)]   # about 15k tokens
        tools = self._schemas(20_000)                                                     # about 10k tokens
        size_tools = lambda ts: CompactionStrategy.estimate_fixed_overhead([], ts)        # noqa: E731
        assert SIZE(prompt) < 20_000 < SIZE(prompt) + size_tools(tools)
        counting = ReplayGuard(target_tokens=20_000, size=SIZE, tools_size=size_tools)
        sent = asyncio.run(counting.before_call(prompt, tools=tools))
        assert SIZE(sent) + size_tools(tools) <= 20_000 and any("ALREADY RAN" in o for o in _outputs(sent))
        blind = ReplayGuard(target_tokens=20_000, size=SIZE)
        assert asyncio.run(blind.before_call(prompt, tools=tools)) == prompt, "without the schemas it was left whole"

    def test_the_strategys_guard_counts_the_schemas_of_a_catalogue_that_nearly_fills_the_window(self) -> None:
        """The builder shape: a fixed part of about 22k tokens in a 32k window. A guard that targets the messages
        alone allows 14k tokens of them on top of the fixed part, 36k against the window."""
        strategy = CompactionStrategy()
        model = ResolvedModel(
            profile_id="p", provider_id="prov", model_name="m", context_length=32_000, config=ModelProfileConfig(),
        )
        prompt = [_user("go"), _call(0), _result(0, 24_000), _call(1), _result(1, 24_000)]    # about 12k tokens
        tools = self._schemas(44_000)                                                         # about 22k tokens
        assert SIZE(prompt) < strategy.reduced_target(model), "the messages alone fit the old target"
        sent = asyncio.run(strategy.replay_guard(model).before_call(prompt, tools=tools))
        # (it reduces what it can: the newest result keeps a floor, so not to the target itself)
        assert SIZE(sent) < SIZE(prompt) // 2, "but with the schemas they are cut: the window has no room for them"
        assert all("ALREADY RAN" in o or len(o) < 24_000 for o in _outputs(sent)) and any("ALREADY RAN" in o for o in _outputs(sent))


class TestCapNewestRound:
    def test_only_the_newest_round_is_cut_and_the_older_ones_are_left_alone(self) -> None:
        rounds = [_call(0), _result(0, 40_000), _call(1), _result(1, 400_000)]
        out = cap_newest_round(rounds, cap_tokens=5_000, size=SIZE)
        assert out[:2] == rounds[:2], "the older round is untouched"
        assert SIZE(out[2:]) <= 5_000 and "ALREADY RAN" in _outputs(out)[1]
        assert [m.role for m in out] == [m.role for m in rounds], "the call keeps its result"

    def test_a_newest_round_within_the_cap_changes_nothing(self) -> None:
        rounds = [_call(0), _result(0, 400_000), _call(1), _result(1, 400)]
        assert cap_newest_round(rounds, cap_tokens=5_000, size=SIZE) == rounds

    def test_a_cap_of_zero_reduces_the_newest_round_to_its_placeholders(self) -> None:
        out = cap_newest_round([_call(0), _result(0, 400_000)], cap_tokens=0, size=SIZE)
        assert len(_outputs(out)[0]) < 1_000 and "ALREADY RAN" in _outputs(out)[0]

    def test_parallel_results_stay_with_their_call(self) -> None:
        call = Message(role="assistant", parts=[
            ToolCallPart(id="c0", name="exec", arguments={}), ToolCallPart(id="c1", name="exec", arguments={}),
        ])
        results = Message(role="tool", parts=[
            ToolResultPart(id="c0", output="a" * 200_000), ToolResultPart(id="c1", output="b" * 200_000),
        ])
        out = cap_newest_round([call, results], cap_tokens=3_000, size=SIZE)
        assert [m.role for m in out] == ["assistant", "tool"] and len(out[1].parts) == 2
        assert SIZE(out) <= 3_500

    def test_the_input_is_not_modified_and_no_rounds_is_no_rounds(self) -> None:
        rounds = [_call(0), _result(0, 400_000)]
        cap_newest_round(rounds, cap_tokens=0, size=SIZE)
        assert _outputs(rounds) == ["a" * 400_000]
        assert cap_newest_round([], cap_tokens=0, size=SIZE) == []


class TestTheNewestRoundCap:
    MODEL = ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m", context_length=32_000, config=ModelProfileConfig(),
    )

    def test_it_is_what_the_budget_leaves_after_the_fixed_part_the_input_and_a_full_summary(self) -> None:
        strategy = CompactionStrategy()
        budget = strategy._effective_budget(self.MODEL)  # noqa: SLF001
        cap = strategy.newest_round_cap(self.MODEL, fixed_overhead=10_000, protected_tokens=500)
        assert cap == budget - 10_000 - 500 - strategy.summary_max_tokens

    def test_it_is_zero_when_the_fixed_part_nearly_fills_the_window(self) -> None:
        """The builder shape (22,013 against a budget of 23,808): nothing is left, so the round is reduced to its
        placeholders instead of the compaction declaring it protected_over_budget."""
        assert CompactionStrategy().newest_round_cap(self.MODEL, fixed_overhead=22_013, protected_tokens=100) == 0


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

    def test_a_recorded_prune_set_lands_on_its_result_when_the_history_holds_an_identical_pair(self) -> None:
        """The set keys a result by id, output hash and OCCURRENCE in the prompt it was recorded against. The
        history holds an identical pair (adapters mint call_0 per stream, and a tool can return the same text), so
        the round's result is occurrence #1 there but #0 among the rounds alone: applied to the rounds alone the
        recorded reduction missed it and the raw result was persisted."""
        history = [_user("go"), _call(0), _result(0, 5_000)]
        rounds = [_call(0), _result(0, 5_000)]
        recorded = PruneSet(omitted=frozenset({result_keys([*history, *rounds])[(4, 0)]}))   # the round's own result
        alone = reduce_for_persist(rounds, sticky=recorded, target_tokens=10**9, size=SIZE)
        assert _outputs(alone) == _outputs(rounds), "the control: without the context the reduction misses"
        with_context = reduce_for_persist(rounds, sticky=recorded, target_tokens=10**9, size=SIZE, context=history)
        assert "ALREADY RAN" in _outputs(with_context)[0] and len(with_context) == len(rounds)

    def test_the_context_is_not_returned_and_is_not_what_the_forced_cut_trims(self) -> None:
        history = [_user("go"), _call(0), _result(0, 400_000)]                    # a big result in the HISTORY
        rounds = [_call(1), _result(1, 200_000)]
        out = reduce_for_persist(rounds, sticky=PruneSet(), target_tokens=5_000, size=SIZE, context=history)
        assert len(out) == len(rounds) and "ALREADY RAN" in _outputs(out)[0], "the cut is for the rounds"
        assert SIZE(out) <= 5_500


class TestKeptRounds:
    def test_the_newest_units_the_compaction_kept_whole_are_counted_from_the_end(self) -> None:
        rounds = [_call(0), _result(0, 5), _call(1), _result(1, 5), _call(2), _result(2, 5)]
        compacted = [_user("q"), Message(role="assistant", parts=[TextPart(text="S")]), *rounds[4:]]
        assert kept_rounds(compacted, rounds) == 1
        assert kept_rounds([_user("q"), *rounds[2:]], rounds) == 2
        assert kept_rounds([_user("q"), *rounds], rounds) == 3

    def test_none_when_the_newest_round_was_not_kept(self) -> None:
        rounds = [_call(0), _result(0, 5), _call(1), _result(1, 5)]
        assert kept_rounds([_user("q"), Message(role="assistant", parts=[TextPart(text="S")])], rounds) == 0
        assert kept_rounds([_user("q"), *rounds[:2]], rounds) == 0, "a kept OLDER round is not the newest"

    def test_no_rounds_is_none(self) -> None:
        assert kept_rounds([_user("q")], []) == 0
