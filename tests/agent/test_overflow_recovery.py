"""invoke's hard-overflow recovery fires on a real overflow and ONLY on a real overflow.

The recovery force-compacts the history (a destructive rewrite: the persisted head becomes a summary) and
replays the turn. Two defects made it wrong in both directions:

* a bare ``max_tokens`` needle sent every Anthropic output-cap 400 ("max_tokens: N > M ... maximum allowed
  number of output tokens") into the compaction, which then hit the same 400 on the replay, on EVERY turn;
* Ollama and Gemini open the request lazily, so their overflow is YIELDED as ``Error(code="bad_request")``
  and surfaces as a ``TurnStreamFailure``, which the ``except BadRequestError`` handler never saw.

These tests drive the real executor with a scripted LLM and a spy compaction strategy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.agent.base import _BaseAgentExecutor
from primer.agent.compaction import CompactedTurn, CompactionStrategy
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    Error,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    TurnStreamFailure,
)
from primer.model.except_ import BadRequestError
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel

MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)

ANTHROPIC_OUTPUT_CAP = (
    "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens for "
    "claude-sonnet-4-20250514"
)
OPENAI_UNSUPPORTED = (
    "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead."
)
OVERFLOW = "This model's maximum context length is 128000 tokens. However, your messages resulted in 130211 tokens."
GEMINI_OVERFLOW = "The input token count (1100000) exceeds the maximum number of tokens allowed (1048575)."


class _SpyCompaction(CompactionStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.forced = 0

    async def maybe_compact(self, **_kwargs):
        return None

    async def force_compact(self, **_kwargs) -> CompactedTurn:
        self.forced += 1
        summary = Message(role="assistant", parts=[TextPart(text="SUMMARY OF THE EARLIER CONVERSATION")])
        return CompactedTurn(
            new_messages=[summary], summary_message=summary,
            estimated_tokens_before=900, estimated_tokens_after=40,
        )


class _Manager:
    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return []


class _Executor(_BaseAgentExecutor):
    def __init__(self, llm, compaction: _SpyCompaction) -> None:
        super().__init__(
            agent=Agent(id="ag", description="x", model=AgentModel(profile_id="p--m")),
            llm=llm, llm_model=MODEL, tool_manager=_Manager(), compaction=compaction,  # type: ignore[arg-type]
        )
        self.history = [
            Message(role="user", parts=[TextPart(text="an earlier question")]),
            Message(role="assistant", parts=[TextPart(text="an earlier answer")]),
        ]
        self.persisted: list[list[Message]] = []
        self.replaced: list[list[Message]] = []

    async def _load_history(self) -> list[Message]:
        return list(self.history)

    async def _persist_turn(self, turn_messages: list[Message]) -> None:
        self.persisted.append(turn_messages)

    async def _replace_compacted_head(self, compacted, *, summary_message=None, tokens_before=0, tokens_after=0):
        self.replaced.append(compacted)
        self.history = compacted


class _FailsThenAnswers:
    """The first ``failures`` calls fail the way the provider does; later calls answer."""

    def __init__(self, *, raises: Exception | None = None, yields: Error | None = None, failures: int = 1) -> None:
        self.raises, self.yields, self.failures = raises, yields, failures
        self.calls = 0

    async def stream(self, **_kwargs) -> AsyncIterator[StreamEvent]:
        # An async generator, like the real adapters: a raise surfaces on the first iteration.
        self.calls += 1
        failing = self.calls <= self.failures
        if failing and self.raises is not None:
            raise self.raises
        if failing:
            yield self.yields
            return
        yield TextDelta(text="all good", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _invoke(executor: _Executor) -> list[StreamEvent]:
    return [ev async for ev in executor.invoke([Message(role="user", parts=[TextPart(text="go")])])]


# --- false positives: an output-cap / parameter 400 must NOT destroy the history -----------------------


@pytest.mark.parametrize("message", [ANTHROPIC_OUTPUT_CAP, OPENAI_UNSUPPORTED], ids=["anthropic", "openai"])
async def test_an_output_cap_400_does_not_force_compact(message: str) -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(message, status_code=400), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(BadRequestError, match="max_tokens"):
        await _invoke(executor)

    assert spy.forced == 0, "an output-cap 400 must not summarise the history away"
    assert executor.replaced == [], "the persisted history must be left alone"
    assert llm.calls == 1, "and the turn must not be replayed against the same 400"


async def test_a_yielded_output_cap_400_does_not_force_compact_either() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="bad_request", message=ANTHROPIC_OUTPUT_CAP, fatal=True), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(TurnStreamFailure):
        await _invoke(executor)

    assert spy.forced == 0 and executor.replaced == [] and llm.calls == 1


# --- true positives: a real overflow still recovers, whichever way the provider reports it --------------


async def test_a_raised_overflow_still_force_compacts_and_replays() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(OVERFLOW, code="context_length_exceeded", status_code=400))
    executor = _Executor(llm, spy)

    events = await _invoke(executor)

    assert spy.forced == 1 and len(executor.replaced) == 1
    assert llm.calls == 2
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_a_yielded_overflow_force_compacts_and_replays() -> None:
    """Ollama and Gemini: the 400 is yielded as Error(code='bad_request'), then raised as a TurnStreamFailure."""
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True))
    executor = _Executor(llm, spy)

    events = await _invoke(executor)

    assert spy.forced == 1 and len(executor.replaced) == 1
    assert llm.calls == 2
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_the_recovery_is_attempted_once_not_in_a_loop() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(OVERFLOW, status_code=400), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(BadRequestError):
        await _invoke(executor)

    assert spy.forced == 1 and llm.calls == 2


async def test_a_yielded_failure_that_is_not_an_overflow_still_propagates() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="rate_limit", message="slow down", fatal=True), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(TurnStreamFailure):
        await _invoke(executor)

    assert spy.forced == 0 and llm.calls == 1
