"""An overflow delivered as a YIELDED ``Error`` is recovered like a raised one, and ends the turn with one terminal record.

Ollama and Gemini open the request lazily, so a prompt that does not fit arrives as a fatal ``Error`` event on the
stream's first iteration instead of a raised ``BadRequestError``. Yielded, that event is streamed to subscribers and
recorded as the turn's ERROR, a terminal record: a turn that then recovers (a forced compaction, a replay) would
carry it and then end with a real terminal after it. The loop therefore holds an error-only overflow stream back when
its caller recovers (``intercept_context_overflow``) and raises ``TurnStreamOverflow``; the executor turns that into
the recovery a raised ``BadRequestError`` gets. Only an error-only stream is held back: content streamed before the
error is already out, and a non-overflow error is not this recovery's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, Error, ExtendedEvent, Message, StreamEvent, TextDelta, TextPart, Tool, ToolResultPart, TurnStreamFailure,
    TurnStreamOverflow, _LlmCall,
)
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests._support.off_golden import FnLLM, open_session, run_turn, text_events
from tests.agent.test_overflow_replay_characterisation import (
    POSIX, QUESTION, _call, _counter, _has_result, _is_summariser, _reload, _seed, _text, _tool_ids,
)

# What Gemini sends for a prompt over its limit, and the code its adapter gives it.
GEMINI = "The input token count (130000) exceeds the maximum number of tokens allowed (128000)."


def _overflow() -> list[StreamEvent]:
    return [Error(code="bad_request", message=GEMINI, fatal=True)]


class TestTheLoop:
    """``run_agent_turn``: the held-back error is opt-in, so a caller that cannot recover is unchanged."""

    MODEL = ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
    )
    AGENT = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"))

    class _Manager:
        def is_notifying(self, tool_name: str) -> bool:
            return False

        async def list_tools(self, *, principal=None) -> list[Tool]:
            return []

        async def execute(self, call, *, principal=None):
            return ToolResultPart(id=call.id, output="ok", error=False)

    class _LLM:
        def __init__(self, events: list[StreamEvent]) -> None:
            self._events = events

        def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
            async def gen() -> AsyncIterator[StreamEvent]:
                for event in self._events:
                    yield event

            return gen()

    async def _run(self, events: list[StreamEvent], **kwargs):
        seen: list = []
        failure = None
        try:
            async for event in run_agent_turn(
                agent=self.AGENT, llm=self._LLM(events), llm_model=self.MODEL, tool_manager=self._Manager(),
                prompt=[Message(role="user", parts=[TextPart(text="hi")])], **kwargs,
            ):
                seen.append(event)
        except TurnStreamFailure as exc:
            failure = exc
        return seen, failure

    def test_by_default_the_error_is_yielded_and_the_turn_fails_as_it_always_did(self) -> None:
        seen, failure = asyncio.run(self._run(_overflow()))
        assert [e for e in seen if isinstance(e, Error)], "the Error reached the caller"
        assert type(failure) is TurnStreamFailure

    def test_when_the_caller_recovers_the_error_is_held_back_and_the_telemetry_still_lands(self) -> None:
        seen, failure = asyncio.run(self._run(_overflow(), intercept_context_overflow=True))
        assert [e for e in seen if isinstance(e, Error)] == [], "a yielded Error is a terminal record: not before a recovery"
        calls = [e.extended for e in seen if isinstance(e, ExtendedEvent) and isinstance(e.extended, _LlmCall)]
        assert [c.status for c in calls] == ["error"], "the call did fail, and its trace row says so"
        assert type(failure) is TurnStreamOverflow and failure.error.message == GEMINI
        assert failure.ended_detail_code == "bad_request", "still a TurnStreamFailure to anything that handles those"

    def test_content_that_was_streamed_before_the_error_is_not_held_back(self) -> None:
        events = [TextDelta(text="partial", index=0), *_overflow()]
        seen, failure = asyncio.run(self._run(events, intercept_context_overflow=True))
        assert [e for e in seen if isinstance(e, Error)], "it is already out, and the turn fails with its ERROR"
        assert type(failure) is TurnStreamFailure

    @pytest.mark.parametrize("code", ["rate_limit", "server_error", "network_error", None])
    def test_an_error_that_is_not_an_overflow_is_never_held_back(self, code) -> None:
        events = [Error(code=code, message=GEMINI, fatal=True)]    # even with an overflow's words
        seen, failure = asyncio.run(self._run(events, intercept_context_overflow=True))
        assert [e for e in seen if isinstance(e, Error)] and type(failure) is TurnStreamFailure

    def test_a_non_fatal_error_is_not_an_overflow(self) -> None:
        seen, failure = asyncio.run(self._run(
            [Error(code="bad_request", message=GEMINI, fatal=False), Done(stop_reason="stop", raw_reason="stop")],
            intercept_context_overflow=True,
        ))
        assert [e for e in seen if isinstance(e, Error)] and type(failure) is TurnStreamFailure, (
            "a non-fatal Error does not classify as an overflow: the turn fails as it always did"
        )


@POSIX
class TestTheExecutor:
    async def test_a_yielded_overflow_after_a_tool_round_is_recovered_and_the_turn_ends_once(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return _overflow()                    # YIELDED, not raised
                return text_events("done")

            events: list = []
            await run_turn(session, FnLLM(fn), collect=events)
            assert [e for e in events if isinstance(e, Error)] == [], "no ERROR before the turn's real end"
            assert [e.stop_reason for e in events if isinstance(e, Done)] == ["tool_use", "stop"]
            assert isinstance(events[-1], Done) and events[-1].stop_reason == "stop"
            assert _counter(workspace) == ["a"], "the tool ran once: the replay continued the turn"
            shown = await _reload(session)
            assert _tool_ids(shown) == (["call_a"], ["call_a"]) and _text(shown[-1]) == "done"
            assert [_text(m) for m in shown].count(QUESTION) == 1
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_yielded_overflow_on_the_first_call_is_recovered_too(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            state = {"overflowed": False}

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not state["overflowed"]:
                    state["overflowed"] = True
                    return _overflow()
                return text_events("done")

            events: list = []
            llm = FnLLM(fn)
            await run_turn(session, llm, collect=events)
            assert [e for e in events if isinstance(e, Error)] == []
            assert len([c for c in llm.calls if _is_summariser(c["kwargs"])]) == 1, "one forced compaction"
            assert _text((await _reload(session))[-1]) == "done"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_a_second_yielded_overflow_ends_the_turn_by_name_with_no_error_event_and_the_round_recorded_once(
        self, tmp_path,
    ) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)

            def fn(n, messages, kwargs):
                if _is_summariser(kwargs):
                    return text_events("SUMMARY")
                if not _has_result(messages, "call_a"):
                    return _call("call_a", "a")
                return _overflow()                        # rejected on every call after round a

            events: list = []
            with pytest.raises(ContextOverflowUnrecoverable) as failed:
                await run_turn(session, FnLLM(fn), collect=events)
            error = failed.value
            assert [e for e in events if isinstance(e, Error)] == [], "the dispatch writes the one terminal record"
            assert isinstance(error.__cause__, BadRequestError) and GEMINI in error.__cause__.message
            assert error.__cause__.code == "bad_request" and error.ended_detail_code == "context_overflow_unrecoverable"
            assert (error.forced_compaction, error.replay_attempted, error.persisted_rounds) == (True, True, 1)
            assert _counter(workspace) == ["a"]
            assert _tool_ids(await _reload(session)) == (["call_a"], ["call_a"])
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_content_before_the_error_fails_the_turn_with_its_error_record_and_no_compaction(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(lambda n, messages, kwargs: [TextDelta(text="partial", index=0), *_overflow()])
            events: list = []
            with pytest.raises(TurnStreamFailure) as failed:
                await run_turn(session, llm, collect=events)
            assert type(failed.value) is TurnStreamFailure
            assert [e for e in events if isinstance(e, Error)], "the partial answer is out, so its ERROR is recorded"
            assert [c for c in llm.calls if _is_summariser(c["kwargs"])] == [], "no compaction for a partial answer"
        finally:
            await session.aclose()
            await backend.aclose()

    async def test_an_error_that_is_not_an_overflow_is_unchanged(self, tmp_path) -> None:
        backend, workspace, session = await open_session(tmp_path)
        try:
            await _seed(workspace, session)
            llm = FnLLM(lambda n, messages, kwargs: [Error(code="rate_limit", message="slow down", fatal=True)])
            events: list = []
            with pytest.raises(TurnStreamFailure) as failed:
                await run_turn(session, llm, collect=events)
            assert failed.value.ended_detail_code == "rate_limit"
            assert [e for e in events if isinstance(e, Error)] and len(llm.calls) == 1
        finally:
            await session.aclose()
            await backend.aclose()
