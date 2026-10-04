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
import tempfile
from pathlib import Path

import pytest

import tests._support.off_golden as g
from primer.agent.compaction import CompactionStrategy
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import Done, Message, TextDelta, TextPart, Tool
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


def _maybe(strategy, *, history, fixed, window, new_messages=None, summariser=None):
    summariser = summariser or _Summariser()
    result = asyncio.run(strategy.maybe_compact(
        agent=g.make_agent(), llm=summariser, model=_model(window), history=history,
        new_messages=new_messages or [], fixed_overhead=fixed,
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

    def test_the_outcomes_are_counted_including_the_skip(self) -> None:
        from primer.observability import metrics

        metrics.reset_for_test()
        _maybe(CompactionStrategy(), history=_history(1_000), fixed=self.FIXED, window=self.WINDOW)
        _maybe(CompactionStrategy(), history=_history(1_000), fixed=24_000, window=self.WINDOW)
        got = {o: metrics.registry.get_sample_value("compaction_outcomes_total", {"outcome": o}) or 0.0
               for o in ("skipped", "unreducible")}
        assert got == {"skipped": 1.0, "unreducible": 1.0}


def _run(coro_factory):
    async def _main():
        with tempfile.TemporaryDirectory(prefix="fixed-") as tmp:
            return await coro_factory(Path(tmp))

    return asyncio.run(_main())


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
