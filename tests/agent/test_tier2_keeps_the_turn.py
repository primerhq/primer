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
import json
import logging
from pathlib import Path

import pytest

import tests._support.off_golden as g
from tests._support.scratch_dir import run_in_scratch_dir
from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

QUESTION = "THE QUESTION THIS TURN MUST ANSWER"


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


def _executor(session, llm) -> WorkspaceAgentExecutor:
    manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    return WorkspaceAgentExecutor(
        agent=g.make_agent(), llm=llm, llm_model=g.make_model(), tool_manager=manager,
        session=session, compaction=CompactionStrategy(),
    )


async def _history(session, llm) -> list[Message]:
    return await _executor(session, llm)._read_messages_jsonl()


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
    return run_in_scratch_dir(coro_factory, prefix="tier2-")


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


def _file_lines(workspace, session) -> list[dict]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _markers(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line.get("kind") == "compaction_marker"]


class TestVerdictsAndSteers:
    """The compaction's verdict is in the session record, and a line written during a compaction is not lost."""

    def test_a_marker_records_a_summarised_verdict_and_the_trigger(self) -> None:
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                llm.extend([g.Events(g.text_events("SUMMARY")), g.Events(g.text_events("the answer"))])
                await g.run_turn(session, llm)
                return _markers(_file_lines(workspace, session))
            finally:
                await session.aclose()
                await backend.aclose()

        (marker,) = _run(scenario)
        assert marker["payload"]["outcome"] == "summarised" and marker["payload"]["unreducible"] is None
        assert marker["payload"]["trigger_tokens"] == int(0.9 * (100_000 - 8_192))

    def test_a_compaction_that_summarised_and_is_still_over_the_trigger_says_so_in_its_marker(self) -> None:
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                huge = "s" * 400_000  # a summary that alone is over the trigger, in both passes
                llm.extend([g.Events(g.text_events(huge)), g.Events(g.text_events(huge)), g.Events(g.text_events("the answer"))])
                await g.run_turn(session, llm)
                return _markers(_file_lines(workspace, session)), len(llm.calls)
            finally:
                await session.aclose()
                await backend.aclose()

        markers, calls = _run(scenario)
        assert calls == 3, "summary, one bounded escalation, then the turn"
        assert len(markers) == 1, "exactly one marker per compaction, written for the result that is returned"
        assert (markers[0]["payload"]["outcome"], markers[0]["payload"]["unreducible"]) == ("insufficient", "over_trigger")

    def test_an_unreducible_compaction_writes_no_marker_and_a_compaction_note_event_is_yielded(self) -> None:
        from primer.model.chat import ExtendedEvent, _CompactionNote

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            events: list = []
            try:
                await g.append_messages(workspace, session, g.user_message("x" * 400_000))  # 100k tokens, unanswered
                llm.extend([g.Events(g.text_events("the answer"))])
                await g.run_turn(session, llm, collect=events)
                return _markers(_file_lines(workspace, session)), len(llm.calls), events
            finally:
                await session.aclose()
                await backend.aclose()

        markers, calls, events = _run(scenario)
        assert markers == [] and calls == 1, "nothing summarised: no marker and no summariser call"
        notes = [e.extended for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _CompactionNote)]
        assert [(n.outcome, n.reason) for n in notes] == [("unreducible", "empty_head")]

    def test_a_steer_written_mid_turn_before_an_overflow_compaction_survives_it(self) -> None:
        """The steer is a Message line written after the turn's history snapshot and before the forced compaction's
        marker. The marker folds every line before it, so the writer must carry the line into the marker's tail."""
        from primer.model.except_ import BadRequestError

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(f"q{i}"), g.assistant_message(f"a{i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                entered, release = asyncio.Event(), asyncio.Event()
                overflow = BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")
                llm.extend([g.Gate(entered, release, [], error=overflow), g.Events(g.text_events("SUMMARY")), g.Events(g.text_events("done"))])
                task = asyncio.create_task(g.run_turn(session, llm))
                await asyncio.wait_for(entered.wait(), timeout=30)
                await session.append_instruction("MID-TURN-STEER")
                release.set()
                await asyncio.wait_for(task, timeout=30)
                return [_text(m) for m in await _history(session, llm)]
            finally:
                await session.aclose()
                await backend.aclose()

        texts = _run(scenario)
        assert "MID-TURN-STEER" in texts, "the steer was folded into the summary and is gone from the next turn"
        assert texts.index(QUESTION) < texts.index("MID-TURN-STEER") < texts.index("done")

    def test_a_steer_drained_after_the_proactive_marker_survives_the_forced_one_in_the_same_turn(self) -> None:
        """Marker 1 (proactive) is written, the steer deferred during its window is drained AFTER it, and then the
        turn's call overflows and a second marker (forced) is written from an in-memory history that does not hold
        the steer. The second marker must carry it, or it is folded into a summary that never saw it. (The reader
        cannot catch this: what a marker carries is decided when it is written.)"""
        from primer.model.except_ import BadRequestError

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                entered, release = asyncio.Event(), asyncio.Event()
                overflow = BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")
                llm.extend([
                    g.Gate(entered, release, g.text_events("SUMMARY-1")),   # the proactive compaction's summariser
                    g.Raise(overflow),                                       # the turn's own call is rejected
                    g.Events(g.text_events("SUMMARY-2")),                    # the forced compaction's summariser
                    g.Events(g.text_events("done")),                         # the replay
                ])
                task = asyncio.create_task(g.run_turn(session, llm))
                await asyncio.wait_for(entered.wait(), timeout=30)
                await session.append_instruction("STEER-DEFERRED-BY-THE-FIRST-WINDOW")  # drained after marker 1
                release.set()
                await asyncio.wait_for(task, timeout=30)
                path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
                markers = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("kind") == "compaction_marker"]
                return [_text(m) for m in await _history(session, llm)], len(markers)
            finally:
                await session.aclose()
                await backend.aclose()

        texts, markers = _run(scenario)
        assert markers == 2, "one proactive and one forced marker"
        assert "STEER-DEFERRED-BY-THE-FIRST-WINDOW" in texts, "the second marker folded the steer into a summary"
        assert texts.index(QUESTION) < texts.index("STEER-DEFERRED-BY-THE-FIRST-WINDOW") < texts.index("done")

    def test_a_line_written_during_the_summariser_call_survives_the_proactive_compaction_too(self) -> None:
        """The compaction window defers ``append_instruction`` steers, but any other writer of a Message line is
        not deferred. The marker folds every line before it, so the proactive path carries the line as well."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                entered, release = asyncio.Event(), asyncio.Event()
                llm.extend([g.Gate(entered, release, g.text_events("SUMMARY")), g.Events(g.text_events("the answer"))])
                task = asyncio.create_task(g.run_turn(session, llm))
                await asyncio.wait_for(entered.wait(), timeout=30)
                await g.append_messages(workspace, session, g.user_message("WRITTEN-DURING-THE-SUMMARISER-CALL"))
                release.set()
                await asyncio.wait_for(task, timeout=30)
                return [_text(m) for m in await _history(session, llm)]
            finally:
                await session.aclose()
                await backend.aclose()

        texts = _run(scenario)
        assert texts.index(QUESTION) < texts.index("WRITTEN-DURING-THE-SUMMARISER-CALL") < texts.index("the answer")

    def test_a_history_rewritten_under_the_compaction_carries_nothing_rather_than_something_wrong(self, caplog) -> None:
        """Another marker landed while the summariser ran, so the reader's history is no longer a continuation of the
        snapshot: lines cannot be told apart by position, and the writer adds none (and says so)."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                for i in range(6):
                    await g.append_messages(workspace, session, g.user_message(chr(ord("A") + i) * g.BIG_USER_CHARS), g.assistant_message(f"reply {i}"))
                await g.append_messages(workspace, session, g.user_message(QUESTION))
                entered, release = asyncio.Event(), asyncio.Event()
                llm.extend([g.Gate(entered, release, g.text_events("SUMMARY")), g.Events(g.text_events("the answer"))])
                task = asyncio.create_task(g.run_turn(session, llm))
                await asyncio.wait_for(entered.wait(), timeout=30)
                other_tail = [{"role": "user" if i % 2 == 0 else "assistant", "parts": [{"type": "text", "text": f"X{i}"}]} for i in range(20)]
                other = {"seq": 9_999, "kind": "compaction_marker", "created_at": "2026-10-05T00:00:00+00:00",
                         "payload": {"summary": "OTHER", "kept_tail_messages": other_tail}}
                await workspace.append_message_line(session.session_id, (json.dumps(other) + "\n").encode())
                release.set()
                await asyncio.wait_for(task, timeout=30)
                return [_text(m) for m in await _history(session, llm)]
            finally:
                await session.aclose()
                await backend.aclose()

        with caplog.at_level(logging.WARNING, logger="primer.agent.workspace_executor"):
            texts = _run(scenario)
        assert any("history changed under a compaction" in r.getMessage() for r in caplog.records)
        assert not any(t.startswith("X") for t in texts), "lines of the other marker were not mistaken for new ones"


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

    def test_every_outcome_is_counted(self) -> None:
        from primer.observability import metrics

        metrics.reset_for_test()

        def counts() -> dict[str, float]:
            return {
                o: metrics.registry.get_sample_value("compaction_outcomes_total", {"outcome": o}) or 0.0
                for o in ("pruned", "summarised", "unreducible", "insufficient")
            }

        self._compact([_msg("user", "x" * 4_000), _msg("user", QUESTION)])
        assert counts() == {"pruned": 0.0, "summarised": 0.0, "unreducible": 1.0, "insufficient": 0.0}, "empty head"
        self._compact([_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", "z" * 400_000)])
        assert counts()["unreducible"] == 2.0, "protected input over the trigger"
        self._compact([_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", QUESTION)],
                      summariser=_Summariser(text="s" * 400_000))
        assert counts()["insufficient"] == 1.0, "summarised, and the summary alone is over the trigger"
        self._compact([m for i in range(6) for m in (_msg("user", f"q{i}"), _msg("assistant", f"a{i}"))] + [_msg("user", QUESTION)])
        assert counts() == {"pruned": 0.0, "summarised": 1.0, "unreducible": 2.0, "insufficient": 1.0}
        history = [m for i in range(4) for m in _round(i, size=90_000)]
        asyncio.run(CompactionStrategy().maybe_compact(
            agent=g.make_agent(), llm=_Summariser(), model=_model(), history=history, new_messages=[],
        ))
        assert counts()["pruned"] == 1.0, "tier 1 sufficed"

    def test_a_protected_suffix_that_fills_the_window_makes_no_summariser_call_and_no_marker(self, caplog) -> None:
        """The current turn's unanswered input alone fills the whole budget: a summary of the rest cannot make the
        prompt fit, so the compaction is unreducible BEFORE any model call (it used to make a summary-of-summary
        call and copy the whole oversized input into every marker)."""
        history = [_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", "z" * 400_000)]
        with caplog.at_level(logging.WARNING, logger="primer.agent.compaction"):
            result, summariser = self._compact(history)
        assert summariser.calls == 0
        assert result.summary_message is None and result.new_messages == history
        assert (result.outcome, result.unreducible) == ("unreducible", "protected_over_budget")
        assert result.trigger_tokens == int(0.9 * (100_000 - 8_192))
        assert any("protected_over_budget" in r.getMessage() for r in caplog.records)

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
        assert result.outcome == "summarised"

    def test_the_re_measure_after_an_escalation_counts_the_fixed_part_like_the_trigger_does(self) -> None:
        """The population of the figure a marker records (``tokens_after``) is the trigger's: the compacted history, the
        new messages AND the fixed part (system prompt and tool schemas). The escalation's own re-measure is a second
        place that sum is made; without the fixed part it records a prompt smaller than the one that is sent, and judges
        ``insufficient`` against the wrong size."""
        history = [
            m for i in range(12) for m in (_msg("user", chr(97 + i) * 40_000), _msg("assistant", f"r{i}"))
        ] + [_msg("user", QUESTION)]
        summariser = _Summariser(text="s" * 80_000)           # the first summary is too big: one bounded escalation
        strategy = CompactionStrategy(tail_turns=8, tail_budget_fraction=1.0)
        fixed = 5_000
        result = asyncio.run(strategy.force_compact(
            agent=g.make_agent(), llm=summariser, model=_model(100_000), history=history, fixed_overhead=fixed,
        ))
        assert summariser.calls == 2, "it escalated"
        assert result.estimated_tokens_after == strategy._estimate_tokens(result.new_messages) + fixed  # noqa: SLF001
        assert result.fixed_overhead_tokens == fixed

    def test_the_escalation_summarises_text_only_so_no_summariser_tool_runs_twice(self) -> None:
        """With ``compaction_tool_access`` on the first pass may call tools; the escalation must not re-run them."""
        from primer.model.chat import Done, TextDelta, ToolCallEnd, ToolCallStart

        class Llm:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def stream(self, *, model, messages, **kwargs):
                self.calls.append(kwargs)
                n = len(self.calls)
                if n == 1:
                    events = [ToolCallStart(id="t1", name="dump", index=0), ToolCallEnd(id="t1", arguments={}, index=0),
                              Done(stop_reason="tool_use", raw_reason="tool_use")]
                elif n == 2:
                    events = [TextDelta(text="s" * 80_000, index=0), Done(stop_reason="stop", raw_reason="stop")]
                else:
                    events = [TextDelta(text="SHORT SUMMARY", index=0), Done(stop_reason="stop", raw_reason="stop")]

                async def gen():
                    for e in events:
                        yield e

                return gen()

        class Tools:
            def __init__(self) -> None:
                self.executed: list[str] = []

            async def list_tools(self, *, principal=None):
                return []

            async def execute(self, call, *, principal=None):
                self.executed.append(call.id)
                return ToolResultPart(id=call.id, output="ok", error=False)

        history = [
            m for i in range(12) for m in (_msg("user", chr(97 + i) * 40_000), _msg("assistant", f"r{i}"))
        ] + [_msg("user", QUESTION)]
        llm, tools = Llm(), Tools()
        result = asyncio.run(CompactionStrategy(tail_turns=8, tail_budget_fraction=1.0).force_compact(
            agent=g.make_agent(), llm=llm, model=_model(), history=history, tool_manager=tools,
        ))
        assert len(llm.calls) == 3 and tools.executed == ["t1"], "the tool ran once, in the first pass only"
        assert "tools" in llm.calls[0] and "tools" not in llm.calls[2], "the escalation call carries no tools"
        assert "SHORT SUMMARY" in _text(result.summary_message)

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

    def test_a_summary_that_alone_is_over_the_trigger_is_insufficient_not_escalated(self, caplog) -> None:
        """Summarised (a marker is written) and still over: the tail is already at its floor, so there is no second
        pass. This is the over-trigger path; the golden no longer reaches it."""
        history = [_msg("user", "old"), _msg("assistant", "old reply"), _msg("user", QUESTION)]
        summariser = _Summariser(text="s" * 400_000)
        with caplog.at_level(logging.WARNING, logger="primer.agent.compaction"):
            result, _ = self._compact(history, summariser=summariser)
        assert summariser.calls == 1
        assert (result.outcome, result.unreducible) == ("insufficient", "over_trigger")
        assert result.summary_message is not None and _text(result.new_messages[-1]) == QUESTION
        assert any("over_trigger" in r.getMessage() for r in caplog.records)

    def test_a_history_of_ended_turns_without_a_final_reply_still_summarises(self) -> None:
        """A Stop, the max_tool_turns cap or an empty completion ends a turn with tool rounds and no final text.
        Those rounds are history once a later user message follows, not pending: treating them as pending made
        every later compaction an empty head and the session could never be compacted again."""
        history = [_msg("user", "u1"), *[m for i in range(30) for m in _round(i, size=12_000)], _msg("user", QUESTION)]
        summariser = _Summariser()
        result = asyncio.run(CompactionStrategy().maybe_compact(
            agent=g.make_agent(), llm=summariser, model=_model(), history=history, new_messages=[],
        ))
        assert result is not None and summariser.calls == 1
        assert result.outcome == "summarised" and _text(result.new_messages[-1]) == QUESTION
        assert result.head_messages_replaced > 0

    def test_a_turn_in_flight_keeps_its_question_and_its_round_and_earlier_ended_turns_are_summarised(self) -> None:
        """A resumed turn, [.., user, assistant tool call, tool result], is the CURRENT turn: all three stay. The
        two turns before it ended without a final reply (a Stop, the tool-turn cap), so their rounds are ordinary
        history: if they were treated as pending the whole history would be protected, the head empty, and this
        would come back unreducible (the regression the first version of the pending rule had)."""
        history = [m for i in range(3) for m in (_msg("user", f"q{i}"), _msg("assistant", f"a{i}"))]
        history += [_msg("user", "ended turn 1"), *_round(0), *_round(1)]       # no final reply
        history += [_msg("user", "ended turn 2"), *_round(2)]                   # no final reply
        history += [_msg("user", QUESTION), *_round(99)]                        # the turn in flight
        result, summariser = self._compact(history)
        assert result.summary_message is not None and result.unreducible is None
        assert [m.role for m in result.new_messages[-3:]] == ["user", "assistant", "tool"]
        assert _text(result.new_messages[-3]) == QUESTION
        summarised = " ".join(_text(m) for m in summariser.requests[0])
        assert "ended turn 1" in summarised, "an ended turn's input went into the summary (it is history, not pending)"


def _answered(messages) -> bool:
    calls = {p.id for m in messages for p in m.parts if isinstance(p, ToolCallPart)}
    results = {p.id for m in messages for p in m.parts if isinstance(p, ToolResultPart)}
    return calls == results


class TestAParkedTurnWithManyRounds:
    """The canonical single-turn runaway (one turn that reads many files) must be shrinkable: only the opening user
    run and the NEWEST round are protected, the rounds between are summarised, and the summary sits after the
    question: [.., user run (verbatim), summary, newest rounds]."""

    @staticmethod
    def _history(rounds: int = 30, size: int = 12_000) -> list[Message]:
        old = [m for i in range(3) for m in (_msg("user", f"old question {i}"), _msg("assistant", f"old answer {i}"))]
        return [*old, _msg("user", QUESTION), *[m for i in range(rounds) for m in _round(i, size=size)]]

    def test_the_early_rounds_are_summarised_and_the_question_and_the_newest_round_stay(self) -> None:
        history = self._history()
        summariser = _Summariser()
        result = asyncio.run(CompactionStrategy().maybe_compact(
            agent=g.make_agent(), llm=summariser, model=_model(), history=history, new_messages=[],
        ))
        assert result is not None and summariser.calls == 1 and result.outcome == "summarised"
        assert result.estimated_tokens_after < int(0.9 * (100_000 - 8_192))
        out = result.new_messages
        assert _text(out[0]) == QUESTION, "the question stays first and verbatim"
        assert _text(out[1]).endswith("THE SUMMARY") and out[1].role == "assistant"
        assert out[-1].parts[0].id == "c29" and out[-2].parts[0].id == "c29", "the newest round is last"
        assert _answered(out), "no tool call without its result"
        assert result.summary_after == 1 and result.head_messages_replaced > 6

    def test_the_summariser_is_shown_the_question_with_the_rounds_it_summarises(self) -> None:
        summariser = _Summariser()
        asyncio.run(CompactionStrategy().maybe_compact(
            agent=g.make_agent(), llm=summariser, model=_model(), history=self._history(), new_messages=[],
        ))
        request = summariser.requests[0]
        assert any(_text(m) == QUESTION for m in request), "the question is read as the context of the rounds"

    def test_a_runaway_with_nothing_before_it_is_shrunk_too(self) -> None:
        history = [_msg("user", QUESTION), *[m for i in range(30) for m in _round(i, size=12_000)]]
        result = asyncio.run(CompactionStrategy().maybe_compact(
            agent=g.make_agent(), llm=_Summariser(), model=_model(), history=history, new_messages=[],
        ))
        assert result is not None and result.outcome == "summarised" and _text(result.new_messages[0]) == QUESTION

    def test_reload_keeps_that_order_and_the_next_turn_is_valid(self) -> None:
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, *self._history())
                llm.extend([g.Events(g.text_events("SUMMARY OF THE EARLY ROUNDS")), g.Events(g.text_events("the answer"))])
                await g.run_turn(session, llm)
                return await _history(session, llm), llm.calls[1]["messages"]
            finally:
                await session.aclose()
                await backend.aclose()

        shown, turn_prompt = _run(scenario)
        texts = [_text(m) for m in shown]
        assert texts[texts.index(QUESTION) + 1].endswith("SUMMARY OF THE EARLY ROUNDS"), "the summary follows the question"
        assert shown[-1].role == "assistant" and _text(shown[-1]) == "the answer"
        assert _answered(shown)
        assert [m["role"] for m in turn_prompt[1:]] == [m.role for m in shown[:-1]], "the turn was sent what a reload returns"


class TestASecondCompactionInTheSameTurn:
    """After compaction 1 of a turn with many rounds the history is [question, summary 1, newest round]. The summary is
    an assistant message that stands between the question and the rounds, so a second compaction in the same turn must
    see through it: the question is still the unanswered input, and summary 1 is folded into summary 2."""

    @staticmethod
    def _rounds(lo: int, hi: int, size: int = 12_000) -> list[Message]:
        return [m for i in range(lo, hi) for m in _round(i, size=size)]

    @staticmethod
    def _assert_the_question_survived(result, summariser) -> None:
        out = result.new_messages
        assert _text(out[0]) == QUESTION, "the question was summarised away by the second compaction"
        assert out[1].role == "assistant" and _text(out[1]).endswith("SUMMARY-2")
        assert result.summary_after == 1
        assert all("SUMMARY-1" not in _text(m) for m in out), "summary 1 was kept next to summary 2 instead of folded"
        request = summariser.requests[0]
        assert any(_text(m) == QUESTION for m in request), "the summariser was not shown the question"
        assert any("SUMMARY-1" in _text(m) for m in request), "the summariser was not shown the first summary"
        assert _answered(out)

    def test_the_second_pass_on_the_history_the_first_one_returned(self) -> None:
        history = [_msg("user", QUESTION), *self._rounds(0, 30)]
        strategy = CompactionStrategy()
        first = asyncio.run(strategy.maybe_compact(
            agent=g.make_agent(), llm=_Summariser("SUMMARY-1"), model=_model(), history=history, new_messages=[],
        ))
        assert first is not None and first.outcome == "summarised" and first.summary_after == 1
        second_summariser = _Summariser("SUMMARY-2")
        second = asyncio.run(strategy.maybe_compact(
            agent=g.make_agent(), llm=second_summariser, model=_model(),
            history=[*first.new_messages, *self._rounds(30, 60)], new_messages=[],
        ))
        assert second is not None and second.outcome == "summarised"
        self._assert_the_question_survived(second, second_summariser)

    def test_across_a_marker_write_and_reconstruct(self) -> None:
        """The same two passes with the history of the second one read back from the file the first one's marker was
        written to: the summary is rebuilt by the reader, so it has to be recognisable there too."""
        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                executor = _executor(session, llm)
                strategy = CompactionStrategy()
                history = [_msg("user", QUESTION), *self._rounds(0, 30)]
                await g.append_messages(workspace, session, *history)
                first = await strategy.maybe_compact(
                    agent=g.make_agent(), llm=_Summariser("SUMMARY-1"), model=_model(), history=history, new_messages=[],
                )
                await executor._replace_compacted_head(
                    first.new_messages, summary_message=first.summary_message, outcome=first.outcome,
                )
                reloaded = await executor._read_messages_jsonl()
                more = self._rounds(30, 60)
                await g.append_messages(workspace, session, *more)
                summariser = _Summariser("SUMMARY-2")
                second = await strategy.maybe_compact(
                    agent=g.make_agent(), llm=summariser, model=_model(), history=[*reloaded, *more], new_messages=[],
                )
                await executor._replace_compacted_head(
                    second.new_messages, summary_message=second.summary_message, outcome=second.outcome,
                )
                return second, summariser, await executor._read_messages_jsonl(), _markers(_file_lines(workspace, session))
            finally:
                await session.aclose()
                await backend.aclose()

        second, summariser, after_two, markers = _run(scenario)
        self._assert_the_question_survived(second, summariser)
        assert [m.get("payload", {}).get("summary_after") for m in markers] == [1, 1]
        assert _text(after_two[0]) == QUESTION and _text(after_two[1]).endswith("SUMMARY-2"), "the reload lost the question"

    def test_a_proactive_then_a_forced_compaction_in_one_invoke(self) -> None:
        """The forced compaction is what an overflow of the turn's own call triggers, in the same invoke as the
        proactive one that left [question, summary 1, newest round] in memory."""
        from primer.model.except_ import BadRequestError

        async def scenario(root):
            backend, workspace, session = await g.open_session(root)
            llm = g.ScriptedLLM()
            llm.session_id = session.session_id
            try:
                await g.append_messages(workspace, session, _msg("user", QUESTION), *self._rounds(0, 30))
                overflow = BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")
                llm.extend([
                    g.Events(g.text_events("SUMMARY-1")),   # the proactive compaction's summariser
                    g.Raise(overflow),                      # the turn's own call is rejected
                    g.Events(g.text_events("SUMMARY-2")),   # the forced compaction's summariser
                    g.Events(g.text_events("the answer")),  # the replay
                ])
                await g.run_turn(session, llm)
                return llm.calls, await _history(session, llm), _markers(_file_lines(workspace, session))
            finally:
                await session.aclose()
                await backend.aclose()

        calls, shown, markers = _run(scenario)
        texts = [_text(m) for m in shown]
        assert len(markers) == 2, "one proactive and one forced marker"
        # the session opens with its own "hello" instruction, so the user run that opens the turn is two messages
        assert texts[:2] == ["hello", QUESTION], "the forced compaction summarised the question"
        assert texts[2].endswith("SUMMARY-2") and not any("SUMMARY-1" in t for t in texts)
        assert [m["payload"].get("summary_after") for m in markers] == [2, 2]
        assert texts[-1] == "the answer"
        assert [m["role"] for m in calls[3]["messages"][1:]] == [m.role for m in shown[:-1]], (
            "the replay was sent something other than what a reload returns (the prompts are fingerprints, not text)"
        )


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

    def test_the_result_says_where_the_summary_goes_when_a_turns_early_rounds_were_summarised(self) -> None:
        history = [_msg("user", "q0"), _msg("assistant", "a0"), _msg("user", QUESTION), *[m for i in range(6) for m in _round(i)]]
        result = self._force(history, tail_budget_fraction=0.0)
        assert result.summary_after == 1
        assert [_text(m) for m in result.kept_tail[:1]] == [QUESTION] and all(m.role != "assistant" or m.parts[0].type == "tool_call" for m in result.kept_tail)
        assert result.new_history[1].role == "assistant" and result.new_history[0] is result.kept_tail[0]

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
