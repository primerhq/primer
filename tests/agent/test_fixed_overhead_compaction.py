"""The fixed part of a prompt (system prompt + tool schemas) counts against the compaction trigger.

It goes out on every call and no history can give it back, so a trigger that compares only the history with
the budget fires late on a small-context model and thrashes when the fixed part alone nearly fills it. These
tests pin: the estimate, that the trigger / the tail budget / the re-measure count it, the budget rules
(unreducible when nothing can fit, skipped when the trigger cannot be reached but the prompt fits, allowed
whenever the prompt does not fit), the live dogfood shape (the builder agent: a fixed part of 22,013 tokens
against a 32,000 window), and that the executor fails a forced compaction that cannot shrink the prompt with a
named error instead of replaying it unchanged. Task 01a10914.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import tests._support.off_golden as g
from tests._support.scratch_dir import run_in_scratch_dir
from primer.agent.base import _BaseAgentExecutor
from primer.agent.compaction import CompactedTurn, CompactionStrategy
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import Done, Message, TextDelta, TextPart, Tool, ToolCallPart, ToolResultPart
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

OVERFLOW = "This model's maximum context length is 100000 tokens, however you requested more"


def _msg(role: str, text: str) -> Message:
    return Message(role=role, parts=[TextPart(text=text)])


def _model(context_length: int) -> ResolvedModel:
    return ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m", context_length=context_length, config=ModelProfileConfig(),
    )


class _Summariser:
    def __init__(self, text: str = "SUMMARY") -> None:
        self.calls = 0
        self.text = text

    def stream(self, **_kwargs):
        self.calls += 1
        text = self.text

        async def _g():
            yield TextDelta(text=text, index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _g()


def _history(tokens: int) -> list[Message]:
    """Answered exchanges totalling about ``tokens``, then a small unanswered question."""
    per_pair = 500
    pairs = max(1, tokens // per_pair)
    return [
        m for i in range(pairs) for m in (_msg("user", chr(97 + i % 26) * (4 * (per_pair - 20))), _msg("assistant", f"r{i}"))
    ] + [_msg("user", "THE QUESTION")]


def _maybe(strategy, *, history, fixed, window, new_messages=None, summariser=None, last_compaction_tokens=None):
    summariser = summariser or _Summariser()
    result = asyncio.run(strategy.maybe_compact(
        agent=g.make_agent(), llm=summariser, model=_model(window), history=history,
        new_messages=new_messages or [], fixed_overhead=fixed, last_compaction_tokens=last_compaction_tokens,
    ))
    return result, summariser


def _tool(name: str, chars: int) -> Tool:
    return Tool(
        id=name, description="d" * chars, toolset_id="ts",
        args_schema={"type": "object", "properties": {"a": {"type": "string", "description": "x" * chars}}},
    )


class TestTheEstimate:
    def test_it_is_the_same_heuristic_the_measurement_script_reports(self) -> None:
        system = [_msg("system", "s" * 4_000)]
        tools = [_tool("a", 400), _tool("b", 4_000)]
        assert CompactionStrategy.estimate_fixed_overhead(system, tools) == count_tokens_char_fallback(messages=system, tools=tools)

    def test_it_grows_with_the_tool_schemas(self) -> None:
        system = [_msg("system", "s")]
        small = CompactionStrategy.estimate_fixed_overhead(system, [_tool("a", 100)])
        large = CompactionStrategy.estimate_fixed_overhead(system, [_tool("a", 100), _tool("b", 8_000)])
        assert large > small + 2_000

    def test_no_tools_and_no_system_prompt_is_nothing(self) -> None:
        assert CompactionStrategy.estimate_fixed_overhead([], []) == 0


class TestTheTriggerCountsIt:
    def test_a_history_under_the_trigger_fires_once_the_fixed_part_is_added(self) -> None:
        window = 100_000  # budget 91,808, trigger 82,627
        history = _history(80_000)
        assert CompactionStrategy._estimate_tokens(history) < 82_627
        none, summariser = _maybe(CompactionStrategy(), history=history, fixed=0, window=window)
        assert none is None and summariser.calls == 0
        fired, summariser = _maybe(CompactionStrategy(), history=history, fixed=3_113, window=window)
        assert fired is not None and fired.fixed_overhead_tokens == 3_113 and summariser.calls == 1

    def test_the_new_messages_count_too(self) -> None:
        history = _history(70_000)
        none, _ = _maybe(CompactionStrategy(), history=history, fixed=0, window=100_000)
        assert none is None
        fired, _ = _maybe(CompactionStrategy(), history=history, fixed=0, window=100_000, new_messages=[_msg("user", "n" * 60_000)])
        assert fired is not None

    def test_the_figures_include_it_and_so_does_the_remeasure(self) -> None:
        history = _history(80_000)
        fixed = 3_113
        result, _ = _maybe(CompactionStrategy(), history=history, fixed=fixed, window=100_000)
        assert result.estimated_tokens_before == CompactionStrategy._estimate_tokens(history) + fixed
        assert result.estimated_tokens_after == CompactionStrategy._estimate_tokens(result.new_messages) + fixed

    def test_the_tail_budget_is_what_is_left_of_the_trigger_after_the_fixed_part(self) -> None:
        """room = trigger - fixed: the same tail that fits with no fixed part is shrunk when 20k of the trigger is taken."""
        history = [m for i in range(10) for m in (_msg("user", chr(97 + i) * 40_000), _msg("assistant", f"r{i}"))] + [_msg("user", "Q")]
        free, _ = _maybe(CompactionStrategy(tail_turns=8), history=history, fixed=0, window=100_000)
        heavy, _ = _maybe(CompactionStrategy(tail_turns=8), history=history, fixed=20_000, window=100_000)
        assert len(heavy.new_messages) < len(free.new_messages)

    def test_a_small_summary_allowance_leaves_room_the_default_one_does_not(self) -> None:
        assert CompactionStrategy()._tail_budget(3_000) == 0, "room under the 4096 allowance: the tail is the floor"
        assert CompactionStrategy(summary_max_tokens=200)._tail_budget(3_000) == 1_500


class TestTheBudgetRules:
    """The dogfood shape: the builder agent's fixed part is 22,013 tokens in a 32,000 window, so the budget is
    23,808 and the trigger 21,427: the fixed part alone is over the trigger, before any history."""

    WINDOW, FIXED = 32_000, 22_013

    def test_a_small_history_is_skipped_not_compacted_every_turn(self) -> None:
        history = _history(1_000)
        result, summariser = _maybe(CompactionStrategy(), history=history, fixed=self.FIXED, window=self.WINDOW)
        assert result is not None and summariser.calls == 0
        assert (result.outcome, result.unreducible) == ("skipped", "cannot_reach_trigger")
        assert result.new_messages == history and result.summary_message is None
        assert result.trigger_tokens == 21_427

    def test_the_summary_allowance_counts_in_the_best_case(self) -> None:
        """A fixed part of 18,000 leaves the best case (18,000 + the protected input) under the 21,427 trigger, but a
        summary of the full 4,096-token allowance on top is over it: the prompt (about 22k) fits the 23,808 budget, so
        compacting cannot get under the trigger and would only run again next turn."""
        history = _history(4_000)
        result, summariser = _maybe(CompactionStrategy(), history=history, fixed=18_000, window=self.WINDOW)
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("skipped", "cannot_reach_trigger")
        small, summariser = _maybe(CompactionStrategy(summary_max_tokens=200), history=history, fixed=18_000, window=self.WINDOW)
        assert summariser.calls == 1 and small.outcome == "summarised", "with a small allowance the trigger is reachable"

    def test_it_is_compacted_once_the_prompt_no_longer_fits_the_window(self) -> None:
        """Always allowed when the projected prompt exceeds the window, even though the trigger is out of reach."""
        history = _history(5_000)
        result, summariser = _maybe(CompactionStrategy(), history=history, fixed=self.FIXED, window=self.WINDOW)
        assert summariser.calls == 1 and result.summary_message is not None
        assert result.outcome == "insufficient", "summarised, and the fixed part alone keeps it over the trigger"

    def test_a_fixed_part_that_fills_the_budget_is_unreducible_whatever_the_history(self) -> None:
        for tokens in (1_000, 50_000):
            result, summariser = _maybe(CompactionStrategy(), history=_history(tokens), fixed=24_000, window=self.WINDOW)
            assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("unreducible", "fixed_over_budget")

    def test_a_fixed_part_that_fills_the_budget_is_named_even_when_there_is_also_nothing_to_summarise(self) -> None:
        """Both are true of a one-message history; the label says the cause that no history change could fix."""
        result, summariser = _maybe(CompactionStrategy(), history=[_msg("user", "Q")], fixed=24_000, window=self.WINDOW)
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("unreducible", "fixed_over_budget")

    def test_fixed_plus_the_protected_input_filling_the_budget_is_unreducible(self) -> None:
        history = [_msg("user", "old"), _msg("assistant", "a"), _msg("user", "q" * 8_000)]  # ~2,000 protected
        result, summariser = _maybe(CompactionStrategy(), history=history, fixed=22_500, window=self.WINDOW)
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("unreducible", "protected_over_budget")

    def test_a_compaction_that_was_asked_for_does_not_skip_itself(self) -> None:
        """The forced path (an overflow, the manual route) knows the prompt does not fit: no skip."""
        history = _history(1_000)
        summariser = _Summariser()
        result = asyncio.run(CompactionStrategy().force_compact(
            agent=g.make_agent(), llm=summariser, model=_model(self.WINDOW), history=history, fixed_overhead=self.FIXED,
        ))
        assert summariser.calls == 1 and result.summary_message is not None

    def test_without_a_fixed_part_the_rules_change_nothing_for_an_ordinary_window(self) -> None:
        history = _history(90_000)
        result, summariser = _maybe(CompactionStrategy(), history=history, fixed=0, window=100_000)
        assert summariser.calls == 1 and result.outcome == "summarised"

    def test_a_compaction_that_bottoms_out_over_the_budget_is_not_repeated_every_turn(self) -> None:
        """The live shape: a 22,013-token fixed part against a 23,808 budget. A compaction leaves the prompt (fixed +
        summary + question) at about 24k, which is still over the budget, so every following turn is over it too. Left
        alone, each of them summarised the summary again (a summariser call per turn, no gain). It must run once, and
        again only once the prompt has grown by a summary allowance beyond what that compaction left."""
        def simulate(*, remember: bool) -> tuple[int, list[str]]:
            summariser = _Summariser(text="s" * 8_000)  # a 2,000-token summary: the result stays over the budget
            history = _history(5_000)[:-1]
            last, verdicts = None, []
            for turn in range(10):
                question = _msg("user", f"question {turn}")
                result, _ = _maybe(
                    CompactionStrategy(), history=history, new_messages=[question], fixed=self.FIXED,
                    window=self.WINDOW, summariser=summariser, last_compaction_tokens=last if remember else None,
                )
                verdicts.append(result.outcome if result is not None else "none")
                if result is not None and result.summary_message is not None:
                    history, last = result.new_messages, result.estimated_tokens_after
                history = [*history, question, _msg("assistant", "a" * 1_200)]  # the turn: about 300 tokens of growth
            return summariser.calls, verdicts

        calls_without, _ = simulate(remember=False)
        assert calls_without == 10, "the control: without the memory it is a summariser call on every turn"
        calls, verdicts = simulate(remember=True)
        assert calls == 1, f"one compaction across ten turns while the prompt grows 300 tokens a turn: {verdicts}"
        assert verdicts[0] == "insufficient" and set(verdicts[1:]) == {"skipped"}

    def test_a_prompt_that_has_grown_by_a_summary_allowance_is_compacted_again(self) -> None:
        last = 24_000                                   # what the last compaction left (over the 23,808 budget)
        grown = _history(7_000)                         # about 29k with the fixed part: past last + 4,096
        result, summariser = _maybe(
            CompactionStrategy(), history=grown, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=last,
        )
        assert summariser.calls == 1 and result.summary_message is not None
        barely = _history(5_000)                        # about 27k: under last + 4,096
        result, summariser = _maybe(
            CompactionStrategy(), history=barely, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=last,
        )
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("skipped", "recently_compacted")
        assert result.new_messages == barely and result.summary_message is None
        allowance, summariser = _maybe(
            CompactionStrategy(summary_max_tokens=200), history=barely, fixed=self.FIXED, window=self.WINDOW,
            last_compaction_tokens=last,
        )
        assert summariser.calls == 1, "the growth that re-enables it is the summary allowance, not a constant"

    def test_a_prompt_that_does_not_fit_the_window_is_compacted_whatever_it_has_grown_by(self) -> None:
        """The memory holds a compaction back only while the provider can still take the prompt: past the budget but
        inside the window it is sent and the overflow path is the net; past the window it would be rejected."""
        last = 31_000                                   # a high figure (inside the window): before is under last + 4,096
        inside = _history(5_000)                        # about 27k with the fixed part: over the budget, inside 32,000
        result, summariser = _maybe(
            CompactionStrategy(), history=inside, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=last,
        )
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("skipped", "recently_compacted")
        beyond = _history(11_000)                       # about 33k: past the window
        result, summariser = _maybe(
            CompactionStrategy(), history=beyond, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=last,
        )
        assert summariser.calls == 1 and result.summary_message is not None

    def test_a_figure_left_under_a_larger_window_does_not_hold_back_a_compaction_this_window_needs(self) -> None:
        """The session's profile was switched to a smaller window after its last compaction (which left the prompt at
        200k under a 262k window). That figure says nothing about this prompt: trusting it skipped a compaction the 32k
        window needs, for "has not grown since"."""
        inside = _history(5_000)                        # about 27k with the fixed part: over the 23.8k budget, inside 32k
        result, summariser = _maybe(
            CompactionStrategy(), history=inside, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=200_000,
        )
        assert summariser.calls == 1 and result.summary_message is not None, "the stale figure was not trusted"
        result, summariser = _maybe(
            CompactionStrategy(), history=inside, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=28_000,
        )
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("skipped", "recently_compacted"), (
            "the control: a figure this window could have left still holds the compaction back"
        )

    def test_the_skip_is_decided_on_the_prompt_that_is_sent_not_the_one_before_the_tier_1_prune(self) -> None:
        """A turn that reads a big file: far over the budget (and the window) before tier 1 prunes the output, and
        about what the last compaction left after it. The marker's ``tokens_after`` and the prompt that goes out are
        both the pruned figure, so that is what the skip compares: on the unpruned one the summariser ran again for a
        gain of a few hundred tokens, on the flagship case of this change."""
        from primer.model.chat import CompactionSummary

        summary = CompactionSummary(role="assistant", parts=[TextPart(text="[earlier conversation compacted]\n\n" + "s" * 2_000)])
        history = [
            summary, _msg("user", "Q"),
            Message(role="assistant", parts=[ToolCallPart(id="c0", name="exec", arguments={"cmd": "cat big"})]),
            Message(role="tool", parts=[ToolResultPart(id="c0", output="x" * 200_000)]),
        ]
        strategy = CompactionStrategy()
        pruned, count = strategy._prune_tool_outputs(  # noqa: SLF001
            history, per_output_threshold=strategy.prune_per_output_tokens, total_threshold=strategy.prune_total_threshold,
        )
        sent = strategy._estimate_tokens(pruned) + self.FIXED  # noqa: SLF001
        unpruned = strategy._estimate_tokens(history) + self.FIXED  # noqa: SLF001
        budget = strategy._effective_budget(_model(self.WINDOW))  # noqa: SLF001
        assert count == 1 and sent < budget < self.WINDOW < unpruned, "the shape: fits after the prune, not before"
        result, summariser = _maybe(
            strategy, history=history, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=sent - 100,
        )
        assert summariser.calls == 0 and result.outcome == "skipped"
        assert result.new_messages == pruned, "and what is returned is the pruned history, the one that is sent"

    def test_a_prompt_that_fits_the_budget_is_skipped_as_before_whatever_the_last_compaction_left(self) -> None:
        """A stale or small figure (the fixed part has grown since) must not turn the skip into a compaction: a prompt
        that fits the window is left alone because its trigger cannot be reached, not because of the memory."""
        history = _history(1_000)                       # about 23.1k with the fixed part: under the 23,808 budget
        result, summariser = _maybe(
            CompactionStrategy(), history=history, fixed=self.FIXED, window=self.WINDOW, last_compaction_tokens=2_000,
        )
        assert summariser.calls == 0 and (result.outcome, result.unreducible) == ("skipped", "cannot_reach_trigger")

    def test_the_memory_never_holds_back_a_prompt_that_the_trigger_can_still_be_reached_from(self) -> None:
        """Only the skip path reads it: where compaction can bring the prompt under the trigger it runs as before."""
        history = _history(90_000)
        result, summariser = _maybe(
            CompactionStrategy(), history=history, fixed=0, window=100_000, last_compaction_tokens=95_000,
        )
        assert summariser.calls == 1 and result.outcome == "summarised"

    def test_the_outcomes_are_counted_including_the_skip(self) -> None:
        from primer.observability import metrics

        metrics.reset_for_test()
        _maybe(CompactionStrategy(), history=_history(1_000), fixed=self.FIXED, window=self.WINDOW)
        _maybe(CompactionStrategy(), history=_history(1_000), fixed=24_000, window=self.WINDOW)
        got = {o: metrics.registry.get_sample_value("compaction_outcomes_total", {"outcome": o}) or 0.0
               for o in ("skipped", "unreducible")}
        assert got == {"skipped": 1.0, "unreducible": 1.0}


def _run(coro_factory):
    return run_in_scratch_dir(coro_factory, prefix="fixed-")


class TestWhenAReplayIsHopeless:
    """``_replay_is_futile``: a forced compaction that came back unreducible fails the turn with a name when the
    replay would send the prompt the provider just rejected: nothing could be summarised and the tier-1 prune changed
    no tool output. Our estimate does not decide it (an estimate under the budget means the heuristic undercounts, and
    an identical prompt is rejected again whatever the estimate says); a prune that did change something makes the
    replay a different prompt, so it gets its call."""

    @staticmethod
    def _forced(
        *, outcome="unreducible", pruned=0, after=150_000, budget: int | None = 100_000, reason="empty_head",
    ) -> CompactedTurn:
        return CompactedTurn(
            new_messages=[], estimated_tokens_before=after, estimated_tokens_after=after, outcome=outcome,
            unreducible=reason if outcome == "unreducible" else None, pruned_tool_outputs=pruned,
            budget_tokens=budget,
        )

    @pytest.mark.parametrize(
        ("case", "futile"),
        [
            (dict(), True),
            (dict(pruned=3), False),                    # the prune changed what would be sent
            (dict(after=50_000), True),                 # the estimate says it fits, but the prompt is the same one
            (dict(budget=None), True),                  # the estimate plays no part
            (dict(outcome="summarised"), False),
            (dict(outcome="insufficient"), False),
            (dict(outcome="skipped"), False),
            (dict(outcome="pruned"), False),
        ],
    )
    def test_the_decision(self, case, futile) -> None:
        assert _BaseAgentExecutor._replay_is_futile(self._forced(**case)) is futile

    @pytest.mark.parametrize(
        ("case", "futile"),
        [
            (dict(reason="empty_head"), True),                              # nothing changed: the same prompt
            (dict(reason="empty_head", pruned=2), False),                   # the prune changed something
            (dict(reason="empty_head", changed=True), False),               # the caller reduced the rounds it ran
            (dict(reason="fixed_over_budget"), True),
            (dict(reason="fixed_over_budget", pruned=2, changed=True), True),
            (dict(reason="protected_over_budget"), True),
            (dict(reason="protected_over_budget", pruned=2, changed=True), True),
            (dict(outcome="summarised", changed=True), False),
        ],
    )
    def test_the_replay_continues_decision(self, case, futile) -> None:
        """The replay-continues recovery: ``changed`` is the caller having reduced the rounds the turn ran before the
        compaction. A fresh session whose first round was huge has nothing to summarise (``empty_head``) and a much
        smaller prompt than the rejected one, so it continues; a fixed or protected part that fills the budget does not
        yield to any reduction of the rest."""
        case = dict(case)
        changed = case.pop("changed", False)
        assert _BaseAgentExecutor._replay_is_futile(self._forced(**case), changed=changed) is futile

    def test_the_strategy_states_the_budget_it_measured_against(self) -> None:
        """The decision reads it from the result, so every verdict has to carry it."""
        strategy = CompactionStrategy()
        window = 100_000
        expected = strategy._effective_budget(_model(window))
        unreducible, _ = _maybe(strategy, history=[_msg("user", "x" * 400_000), _msg("user", "Q")], fixed=0, window=window)
        assert unreducible is not None and unreducible.outcome == "unreducible" and unreducible.budget_tokens == expected
        summarised, _ = _maybe(strategy, history=_history(90_000), fixed=0, window=window)
        assert summarised is not None and summarised.outcome == "summarised" and summarised.budget_tokens == expected
        rounds = [
            m for i in range(4) for m in (
                Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="exec", arguments={})]),
                Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output="x" * 90_000)]),
            )
        ]
        pruned, _ = _maybe(strategy, history=rounds, fixed=0, window=window)
        assert pruned is not None and pruned.outcome == "pruned" and pruned.budget_tokens == expected


class TestTheExecutor:
    def test_the_marker_records_the_fixed_part_the_figures_include(self) -> None:
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message("Q"))
                llm.extend([g.Events(g.text_events("SUMMARY")), g.Events(g.text_events("done"))])
                await g.run_turn(session, llm)
                path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
                (marker,) = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("kind") == "compaction_marker"]
                return marker["payload"], await g.fixed_overhead(session)
            finally:
                await session.aclose()
                await backend.aclose()

        payload, fixed = _run(scenario)
        assert payload["fixed_overhead_tokens"] == fixed and fixed > 2_000, "the workspace tools and the system prompt"

    def test_the_fixed_part_pushes_a_history_over_the_trigger_in_a_live_session(self) -> None:
        """History alone is under the trigger; with the real system prompt and 7 tool schemas it is over."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                fixed = await g.fixed_overhead(session)
                trigger = g.trigger_tokens()
                target_history = trigger - fixed // 2  # under the trigger by itself, over it with the fixed part
                per_pair = 500
                for i in range(target_history // per_pair):
                    await g.append_messages(workspace, session, g.user_message(chr(97 + i % 26) * (4 * (per_pair - 20))), g.assistant_message(f"r{i}"))
                await g.append_messages(workspace, session, g.user_message("Q"))
                assert await g._history_estimate(session) < trigger
                llm.extend([g.Events(g.text_events("SUMMARY")), g.Events(g.text_events("done"))])
                await g.run_turn(session, llm)
                return len(llm.calls)
            finally:
                await session.aclose()
                await backend.aclose()

        assert _run(scenario) == 2, "the summariser ran: the fixed part was counted"

    def test_a_forced_compaction_that_cannot_shrink_the_prompt_fails_with_a_name_and_does_not_replay(self) -> None:
        """The fixed part alone fills a tiny window: replaying the byte-identical prompt would be rejected again."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, g.user_message("q"), g.assistant_message("a"), g.user_message("Q"))
                llm.extend([g.Raise(BadRequestError(OVERFLOW))])
                with pytest.raises(ContextOverflowUnrecoverable) as failed:
                    await g.run_turn(session, llm, llm_model=_model(4_000))
                path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
                markers = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("kind") == "compaction_marker"]
                return failed.value, len(llm.calls), markers
            finally:
                await session.aclose()
                await backend.aclose()

        error, calls, markers = _run(scenario)
        assert calls == 1, "no summariser call and no replay"
        assert error.code == "context_overflow_unrecoverable" and isinstance(error.__cause__, BadRequestError)
        assert "fixed_over_budget" in str(error) and "context window of 4000" in str(error)
        assert markers == []

    def test_a_forced_compaction_that_pruned_a_tool_output_replays_the_pruned_prompt(self) -> None:
        """Nothing can be summarised (the turn is one question and one round), but the forced tier-1 prune shrank a
        tool output: the replay is a different, smaller prompt, so failing now would throw away a turn that fits."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(
                    workspace, session, g.user_message("Q"),
                    Message(role="assistant", parts=[ToolCallPart(id="c0", name="exec", arguments={"command": "ls"})]),
                    Message(role="tool", parts=[ToolResultPart(id="c0", output="x" * 200_000)]),
                )
                llm.extend([g.Raise(BadRequestError(OVERFLOW)), g.Events(g.text_events("done"))])
                await g.run_turn(session, llm)
                return llm.calls
            finally:
                await session.aclose()
                await backend.aclose()

        calls = _run(scenario)

        def result_len(call) -> int:
            return next(p[1] for m in call["messages"] for p in m["parts"] if p[0] == "tool_result")

        assert len(calls) == 2, "the pruned prompt was replayed"
        assert result_len(calls[1]) < result_len(calls[0]) // 10, "the replay carries the placeholder, not the output"

    def test_an_identical_replay_is_not_spent_even_when_our_estimate_says_the_prompt_fits(self) -> None:
        """The provider rejected a prompt our estimate puts far under the budget (an image or a document is a flat
        guess; dense text runs over chars/4). Compaction can change nothing, so the replay would send the same prompt
        and be rejected again: it fails by name, without the second call, and says that the estimate disagrees."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, g.user_message("Q"))
                llm.extend([g.Raise(BadRequestError(OVERFLOW))])        # no second step: a replay would fail the run
                with pytest.raises(ContextOverflowUnrecoverable) as failed:
                    await g.run_turn(session, llm)
                return failed.value, len(llm.calls)
            finally:
                await session.aclose()
                await backend.aclose()

        error, calls = _run(scenario)
        assert calls == 1, "no replay of a byte-identical prompt"
        assert isinstance(error.__cause__, BadRequestError) and error.code == "context_overflow_unrecoverable"
        assert "empty_head" in str(error) and "undercounts" in str(error)

    def test_a_run_of_skipped_compactions_is_noted_in_the_session_record_once(self) -> None:
        """A skip writes no marker, so it was invisible: an agent whose prompt sat between the trigger and the budget
        looked like compaction was broken. One ``compaction_note`` per run, not one per turn. The executor yields the
        note as an event and the dispatch path persists it; here each note is persisted the way dispatch does, and the
        next turn must read it back from the file to know the run has its note."""
        from primer.model.chat import ExtendedEvent, _CompactionNote
        from primer.session.persistence import _CoalesceState, translate_stream_event

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                # 8,192-token window: budget 4,096, trigger 3,686; the real fixed part is about 3,100 tokens, so a
                # 600-token message puts the prompt between them and the trigger cannot be reached
                await g.append_messages(workspace, session, g.user_message("p" * 2_400), g.assistant_message("ok"))
                notes_per_turn, calls = [], 0
                for turn in range(3):
                    await g.append_messages(workspace, session, g.user_message(f"Q{turn}"))
                    llm.extend([g.Events(g.text_events(f"done {turn}"))])
                    events: list = []
                    await g.run_turn(session, llm, llm_model=_model(8_192), collect=events)
                    notes = [e for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _CompactionNote)]
                    notes_per_turn.append([(n.extended.outcome, n.extended.reason) for n in notes])
                    for n in notes:  # what the dispatch path does with it
                        record = translate_stream_event(n, _CoalesceState())
                        await workspace.append_message_line(
                            session.session_id, (record.model_copy(update={"seq": 50 + turn}).model_dump_json() + "\n").encode(),
                        )
                return notes_per_turn, len(llm.calls)
            finally:
                await session.aclose()
                await backend.aclose()

        notes_per_turn, calls = _run(scenario)
        assert calls == 3, "three turns, no summariser call: the compaction skipped itself each time"
        assert notes_per_turn == [[("skipped", "cannot_reach_trigger")], [], []], "noted once for the run, not once per turn"

    def test_an_unreducible_verdict_that_repeats_is_noted_once_and_a_new_reason_is_noted_again(self) -> None:
        """``fixed_over_budget`` (the system prompt and tool schemas alone fill the budget) is the same on every turn
        until the agent changes. It used to write a note, and a WARNING, per turn. The newest note since the last marker
        is what a verdict is compared with: the same one is not repeated, a different one is."""
        from primer.model.chat import ExtendedEvent, _CompactionNote
        from primer.session.persistence import _CoalesceState, translate_stream_event

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                notes_per_turn = []
                for turn in range(3):
                    await g.append_messages(workspace, session, g.user_message(f"Q{turn}"))
                    llm.extend([g.Events(g.text_events(f"done {turn}"))])
                    events: list = []
                    # a 4,096-token window: budget 2,048, under the real fixed part (about 3,100 tokens)
                    await g.run_turn(session, llm, llm_model=_model(4_096), collect=events)
                    notes = [e for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _CompactionNote)]
                    notes_per_turn.append([(n.extended.outcome, n.extended.reason) for n in notes])
                    for n in notes:
                        record = translate_stream_event(n, _CoalesceState())
                        await workspace.append_message_line(
                            session.session_id, (record.model_copy(update={"seq": 50 + turn}).model_dump_json() + "\n").encode(),
                        )
                return notes_per_turn
            finally:
                await session.aclose()
                await backend.aclose()

        assert _run(scenario) == [[("unreducible", "fixed_over_budget")], [], []], "noted once for the run, not every turn"

    def test_the_note_decision_compares_the_outcome_and_the_reason(self) -> None:
        from primer.agent.base import _BaseAgentExecutor
        from primer.agent.compaction import CompactedTurn

        def turn(outcome: str, reason: str) -> CompactedTurn:
            return CompactedTurn(
                new_messages=[], estimated_tokens_before=1, estimated_tokens_after=1, outcome=outcome, unreducible=reason,
            )

        notes = _BaseAgentExecutor._compaction_notes  # noqa: SLF001
        verdict = ("skipped", "cannot_reach_trigger")
        assert notes(turn(*verdict), noted=verdict) == [], "the same verdict again: part of the run"
        assert len(notes(turn(*verdict))) == 1, "nothing noted yet"
        assert len(notes(turn("skipped", "recently_compacted"), noted=verdict)) == 1, "another reason: a new run"
        assert len(notes(turn("unreducible", "cannot_reach_trigger"), noted=verdict)) == 1, "another outcome: a new run"

    def test_a_turn_with_a_tool_round_that_ends_on_a_question_waits_for_the_newest_assistant_message(self) -> None:
        """The turn persisted two assistant messages (the tool call, then the question). The end-of-turn check must look
        at the NEWEST: the first has no text and would read as "no question"."""
        from primer.model.chat import Done, ToolCallEnd, ToolCallStart
        from primer.model.workspace_session import SessionStatus

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, g.user_message("clean it up"))
                llm.extend([
                    g.Events([
                        ToolCallStart(id="c1", name="workspace__exec", index=0),
                        ToolCallEnd(id="c1", arguments={"command": "true", "description": "noop"}, index=0),
                        Done(stop_reason="tool_use", raw_reason="tool_use"),
                    ]),
                    g.Events(g.text_events("Should I delete the old branch too?")),
                ])
                await g.run_turn(session, llm)
                return await session.status()
            finally:
                await session.aclose()
                await backend.aclose()

        assert _run(scenario) == SessionStatus.WAITING

    def test_the_tool_catalogue_is_fetched_once_per_invoke(self) -> None:
        """The compaction needs it for the fixed part; the loop is handed the same list instead of fetching again."""
        class Counting:
            def __init__(self, inner) -> None:
                self._inner, self.list_calls = inner, 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def list_tools(self, **kwargs):
                self.list_calls += 1
                return await self._inner.list_tools(**kwargs)

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            wrapped: list[Counting] = []

            def wrap(manager):
                wrapped.append(Counting(manager))
                return wrapped[0]

            try:
                await g.append_messages(workspace, session, g.user_message("hi"))
                llm.extend([g.Events(g.text_events("done"))])
                await g.run_turn(session, llm, wrap_tools=wrap)
                return wrapped[0].list_calls
            finally:
                await session.aclose()
                await backend.aclose()

        assert _run(scenario) == 1

    def test_a_plain_invoke_reads_the_history_file_twice_not_four_times(self) -> None:
        """The window's snapshot and the newest compaction's state come from ONE read, and the end-of-turn question check
        takes the assistant message the turn just persisted instead of re-reading the whole history to find it: what is
        left is the snapshot and the append (a read-modify-write that must read under the lock). A long session's file is
        megabytes; each read is a full parse."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            reads: list[str] = []
            state = session._state  # noqa: SLF001
            original = state.read_state_file

            async def spy(rel, *args, **kwargs):
                reads.append(rel)
                return await original(rel, *args, **kwargs)

            state.read_state_file = spy
            try:
                await g.append_messages(workspace, session, g.user_message("hi"), g.assistant_message("yo"), g.user_message("Q"))
                llm.extend([g.Events(g.text_events("done"))])
                await g.run_turn(session, llm)
                return [r for r in reads if r.endswith("messages.jsonl")]
            finally:
                await session.aclose()
                await backend.aclose()

        assert len(_run(scenario)) == 2

    def test_a_turn_that_ends_on_a_question_still_waits_for_the_user_without_re_reading_the_history(self) -> None:
        from primer.model.workspace_session import SessionStatus

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, g.user_message("build it"))
                llm.extend([g.Events(g.text_events("Which database do you want me to use?"))])
                await g.run_turn(session, llm)
                return await session.status()
            finally:
                await session.aclose()
                await backend.aclose()

        assert _run(scenario) == SessionStatus.WAITING

