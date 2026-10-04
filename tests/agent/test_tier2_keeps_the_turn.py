"""After a tier-2 compaction the kept tail and the turn's own input must still be there next turn.

End to end on the real ``WorkspaceAgentExecutor`` and a real local workspace session (the scenario
helpers are the ones the ``off`` golden uses), plus the strategy-level rules the fix introduced:

* the tail is bounded by size, never splits a tool call from its results, and never reaches into
  the input the model has not answered yet;
* when nothing can be summarised the compaction says so (``unreducible``) instead of folding the
  question into a summary;
* after compacting, the result is measured again and a prompt still over the trigger is reported.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest

import tests._support.off_golden as g
from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

QUESTION = "THE QUESTION THIS TURN MUST ANSWER"


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


async def _history(session, llm) -> list[Message]:
    manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    executor = WorkspaceAgentExecutor(
        agent=g.make_agent(), llm=llm, llm_model=g.make_model(), tool_manager=manager,
        session=session, compaction=CompactionStrategy(),
    )
    return await executor._read_messages_jsonl()


async def _compacting_turn(root: Path, *, pairs: int = 6, question: str = QUESTION):
    """Six big user/assistant pairs and then the question; one turn compacts and answers it."""
    backend, workspace, session = await g.open_session(root)
    llm = g.ScriptedLLM()
    llm.session_id = session.session_id
    for i in range(pairs):
        await g.append_messages(
            workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"),
        )
    await g.append_messages(workspace, session, g.user_message(question))
    llm.extend([g.Events(g.text_events("SUMMARY OF THE HEAD")), g.Events(g.text_events("the answer"))])
    await g.run_turn(session, llm)
    return backend, session, llm


def _run(coro_factory):
    async def _main():
        with tempfile.TemporaryDirectory(prefix="tier2-") as tmp:
            return await coro_factory(Path(tmp))

    return asyncio.run(_main())


class TestTheNextTurnAfterTierTwo:
    def test_is_handed_the_kept_tail_and_the_question_not_only_the_summary(self) -> None:
        async def scenario(root):
            backend, session, llm = await _compacting_turn(root)
            try:
                return await _history(session, llm)
            finally:
                await session.aclose()
                await backend.aclose()

        shown = _run(scenario)
        texts = [_text(m) for m in shown]
        assert shown[0].role == "assistant" and "SUMMARY OF THE HEAD" in texts[0]
        assert QUESTION in texts, "the question the turn answered is gone from the next turn's history"
        assert texts.index(QUESTION) < texts.index("the answer")
        assert "reply 5" in texts, "the most recent assistant message the compactor kept verbatim is gone"
        assert len(shown) >= 4

    def test_the_compacting_turn_itself_was_sent_the_same_tail_that_is_persisted(self) -> None:
        """What the model saw this turn and what the next turn is handed must agree."""
        async def scenario(root):
            backend, session, llm = await _compacting_turn(root)
            try:
                return await _history(session, llm), llm.calls[1]["messages"]
            finally:
                await session.aclose()
                await backend.aclose()

        shown, sent = _run(scenario)
        assert sent[-1]["role"] == "user", "the compacting turn's prompt must end with the question"
        assert [m["role"] for m in sent[1:]] == [m.role for m in shown[:-1]], (
            "the prompt after the system message is the persisted history without the reply it produced"
        )

    def test_the_tail_keeps_the_question_even_when_the_history_has_no_assistant_message_at_all(self) -> None:
        """Zero assistant messages used to fold the question into the summary and send a prompt with none."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(3):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                llm.extend([g.Events(g.text_events("the answer"))])
                await g.run_turn(session, llm)
                return llm.calls, await _history(session, llm)
            finally:
                await session.aclose()
                await backend.aclose()

        calls, shown = _run(scenario)
        assert len(calls) == 1, "nothing is summarisable, so the only call is the turn's own"
        assert calls[0]["messages"][-1]["role"] == "user", "the prompt must end with the question, not a summary"
        assert QUESTION in [_text(m) for m in shown]


def _msg(role: str, text: str) -> Message:
    return Message(role=role, parts=[TextPart(text=text)])


def _round(i: int, size: int = 400) -> list[Message]:
    return [
        Message(role="assistant", parts=[ToolCallPart(id=f"c{i}", name="exec", arguments={"command": "ls"})]),
        Message(role="tool", parts=[ToolResultPart(id=f"c{i}", output="x" * size)]),
    ]


class _Summariser:
    def __init__(self, text: str = "THE SUMMARY") -> None:
        self.calls = 0
        self.text = text
        self.requests: list[list[Message]] = []

    def stream(self, **kwargs):
        from primer.model.chat import Done, TextDelta

        self.calls += 1
        self.requests.append(kwargs["messages"])
        text = self.text

        async def _g():
            yield TextDelta(text=text, index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return _g()


def _model(context_length: int = 100_000) -> ResolvedModel:
    return ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m",
        context_length=context_length, config=ModelProfileConfig(),
    )


class TestTheStrategy:
    def _compact(self, history, *, context_length=100_000, summariser=None, strategy=None):
        summariser = summariser or _Summariser()
        strategy = strategy or CompactionStrategy()
        result = asyncio.run(
            strategy.force_compact(agent=g.make_agent(), llm=summariser, model=_model(context_length), history=history)
        )
        return result, summariser

    def test_the_unanswered_input_is_never_in_the_summary_request_and_always_in_the_result(self) -> None:
        """Holds before the fix too when the history has enough assistant messages; it guards the size shrink, which
        is what could now reach into the input (four 15k-token pairs overflow the tail budget, so it IS shrunk)."""
        from primer.agent.tail import tail_split

        history = [_msg("user", "old"), _msg("assistant", "old reply"), *[
            m for i in range(5) for m in (_msg("user", chr(97 + i) * 60_000), _msg("assistant", f"r{i}"))
        ], _msg("user", QUESTION)]
        result, summariser = self._compact(history)
        assert result.summary_message is not None
        assert all(QUESTION not in _text(m) for m in summariser.requests[0])
        assert _text(result.new_messages[-1]) == QUESTION
        assert len(result.new_messages) - 1 < len(tail_split(history, tail_turns=4)[1]), "the tail was shrunk"

    def test_a_tool_call_is_never_separated_from_its_results_at_any_tail_size(self) -> None:
        history = [_msg("user", "go"), *_round(0), *_round(1), *_round(2), _msg("assistant", "done"), _msg("user", "next")]
        from primer.agent.tail import split_for_compaction

        for budget in range(0, 400, 7):
            for turns in range(0, 6):
                split = split_for_compaction(
                    history, tail_turns=turns, tail_budget_tokens=budget, size=CompactionStrategy._estimate_tokens,
                )
                for part in (split.head, split.tail):
                    calls = {p.id for m in part for p in m.parts if isinstance(p, ToolCallPart)}
                    results = {p.id for m in part for p in m.parts if isinstance(p, ToolResultPart)}
                    assert calls == results, f"budget={budget} tail_turns={turns}: {calls} vs {results}"

    def test_an_empty_head_is_reported_unreducible_and_nothing_is_summarised(self, caplog) -> None:
        history = [_msg("user", "x" * 4_000), _msg("user", QUESTION)]
        with caplog.at_level(logging.WARNING, logger="primer.agent.compaction"):
            result, summariser = self._compact(history)
        assert summariser.calls == 0
        assert result.summary_message is None
        assert result.unreducible == "empty_head"
        assert result.new_messages == history
        assert any("unreducible" in r.getMessage() for r in caplog.records)

    def test_a_tail_larger_than_its_budget_is_shrunk_before_it_is_kept(self) -> None:
        history = [
            m for i in range(8) for m in (_msg("user", chr(97 + i) * 60_000), _msg("assistant", f"r{i}"))
        ] + [_msg("user", QUESTION)]
        result, _ = self._compact(history)
        trigger = int(0.9 * (100_000 - 8_192))
        assert result.estimated_tokens_after < trigger
        assert result.unreducible is None

    def test_every_unreducible_verdict_is_counted_by_reason(self) -> None:
        from primer.observability import metrics

        metrics.reset_for_test()

        def count(reason: str) -> float:
            return metrics.registry.get_sample_value("compaction_unreducible_total", {"reason": reason}) or 0.0

        self._compact([_msg("user", "x" * 4_000), _msg("user", QUESTION)])
        assert (count("empty_head"), count("over_trigger")) == (1.0, 0.0)
        self._compact([_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", "z" * 400_000)])
        assert (count("empty_head"), count("over_trigger")) == (1.0, 1.0)
        self._compact([m for i in range(6) for m in (_msg("user", f"q{i}"), _msg("assistant", f"a{i}"))] + [_msg("user", QUESTION)])
        assert (count("empty_head"), count("over_trigger")) == (1.0, 1.0), "a compaction that worked counts nothing"

    def test_a_summary_larger_than_its_allowance_gets_one_bounded_escalation(self) -> None:
        """The first split leaves the summary plus the tail over the trigger, so the tail is cut down to what may not
        be summarised and the head is summarised again (once): two summariser calls, never more."""
        history = [
            m for i in range(12) for m in (_msg("user", chr(97 + i) * 40_000), _msg("assistant", f"r{i}"))
        ] + [_msg("user", QUESTION)]
        summariser = _Summariser(text="s" * 80_000)  # about 20k tokens: the model ignored the allowance
        result, _ = self._compact(
            history, summariser=summariser, strategy=CompactionStrategy(tail_turns=8, tail_budget_fraction=1.0),
        )
        trigger = int(0.9 * (100_000 - 8_192))
        assert summariser.calls == 2
        assert [_text(m) for m in result.new_messages[1:]] == [QUESTION]
        assert result.estimated_tokens_after < trigger and result.unreducible is None

    def test_the_tail_budget_leaves_room_for_a_summary_of_the_full_allowance(self) -> None:
        """A summary within its allowance (4096 tokens) plus a tail that just fits the trigger would be over it: the
        budget is cut by the allowance up front, so no second summariser call is needed."""
        history = [
            m for i in range(12) for m in (_msg("user", chr(97 + i) * 40_000), _msg("assistant", f"r{i}"))
        ] + [_msg("user", QUESTION)]
        summariser = _Summariser(text="s" * (4_096 * 4))
        result, _ = self._compact(
            history, summariser=summariser, strategy=CompactionStrategy(tail_turns=9, tail_budget_fraction=1.0),
        )
        assert summariser.calls == 1 and result.unreducible is None
        assert result.estimated_tokens_after < int(0.9 * (100_000 - 8_192))

    def test_the_escalation_is_not_repeated_when_the_tail_is_already_as_small_as_it_may_be(self) -> None:
        history = [_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", "z" * 400_000)]
        summariser = _Summariser()
        result, _ = self._compact(history, summariser=summariser)
        assert summariser.calls == 1 and result.unreducible == "over_trigger"

    def test_the_result_is_measured_again_and_a_prompt_still_over_the_trigger_is_reported(self, caplog) -> None:
        """The unanswered input alone is over the trigger: summarising the rest cannot help, and we say so."""
        history = [_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", "z" * 400_000)]
        with caplog.at_level(logging.WARNING, logger="primer.agent.compaction"):
            result, _ = self._compact(history)
        assert result.unreducible == "over_trigger"
        assert any("unreducible" in r.getMessage() for r in caplog.records)


class TestTheManualAndMixinPath:
    """``force_compact`` in the mixin is what the manual compaction route calls."""

    @staticmethod
    def _force(history, **strategy_kwargs):
        from primer.agent.compaction_mixin import force_compact

        return asyncio.run(
            force_compact(
                llm=_Summariser(), strategy=CompactionStrategy(**strategy_kwargs), history=history,
                compaction_prompt="", model_name="m", context_length=100_000,
            )
        )

    def test_the_result_carries_the_kept_tail_for_the_marker(self) -> None:
        history = [_msg("user", "q0"), _msg("assistant", "a0"), _msg("user", "q1"), _msg("assistant", "a1"), _msg("user", QUESTION)]
        result = self._force(history)
        assert [_text(m) for m in result.kept_tail] == ["a1", QUESTION]
        assert result.new_history[1:] == result.kept_tail
        assert result.unreducible is None

    def test_the_tail_budget_survives_the_clone_the_mixin_makes_for_an_explicit_request(self) -> None:
        """``_clone_strategy_for_apply`` rebuilds the strategy to clamp ``tail_turns``: it must carry the budget too."""
        history = [_msg("user", "q0"), _msg("assistant", "a0"), _msg("user", "q1"), _msg("assistant", "a1"), _msg("user", QUESTION)]
        assert [_text(m) for m in self._force(history, tail_budget_fraction=0.0).kept_tail] == [QUESTION]

    def test_an_idle_history_is_summarised_whole_as_before(self) -> None:
        """What the manual route did and still does: a history ending on an answer folds into the summary alone."""
        history = [_msg("user", "q0"), _msg("assistant", "a0")]
        result = self._force(history)
        assert result.kept_tail == [] and result.head_messages_replaced == 2 and result.unreducible is None

    def test_nothing_to_summarise_is_reported_not_faked(self) -> None:
        result = self._force([_msg("user", QUESTION)])
        assert result.summary_text == "" and result.unreducible == "empty_head"
