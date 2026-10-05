"""invoke's hard-overflow recovery fires on a real overflow and ONLY on a real overflow.

The recovery force-compacts the history (a destructive rewrite: the persisted head becomes a summary) and
replays the turn. Two defects made it wrong in both directions:

* a bare ``max_tokens`` needle sent every Anthropic output-cap 400 ("max_tokens: N > M ... maximum allowed
  number of output tokens") into the compaction, which then hit the same 400 on the replay, on EVERY turn;
* Ollama and Gemini open the request lazily, so their overflow is YIELDED as ``Error(code="bad_request")``
  and used to surface as a ``TurnStreamFailure``, which the ``except BadRequestError`` handler never saw.
  The classifier recognises it and ``invoke`` now recovers it like a raised one: the loop holds the
  ``Error`` back (a yielded one is a terminal record) and raises ``TurnStreamOverflow``. The tests below
  pin that, and that an error that is NOT an overflow is still left alone.

These tests drive the real executor with a scripted LLM and a spy compaction strategy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.agent.base import _BaseAgentExecutor
from primer.agent.compaction import CompactedTurn, CompactionStrategy
from primer.common.context_overflow import is_context_overflow
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
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable
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
    def __init__(self, llm, compaction: _SpyCompaction, *, max_output_tokens: int | None = None) -> None:
        super().__init__(
            agent=Agent(
                id="ag", description="x", model=AgentModel(profile_id="p--m"), max_output_tokens=max_output_tokens,
            ),
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

    async def _replace_compacted_head(self, compacted, **_hook_kwargs):
        # The hook's keyword arguments (summary_message, tokens_*, outcome, unreducible, ...) are the
        # persistence layer's business; this double only needs the compacted history.
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


async def test_a_yielded_overflow_is_recovered_like_a_raised_one_and_no_error_event_is_yielded() -> None:
    """Ollama and Gemini: the 400 is yielded as Error(code='bad_request'). It is held back (a yielded Error is a
    terminal record, and a recovered turn must end with exactly one), the history is force-compacted once and the
    turn continues."""
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True), failures=1)
    executor = _Executor(llm, spy)

    events = await _invoke(executor)

    assert spy.forced == 1 and llm.calls == 2, "one forced compaction, then the replay answered"
    assert [e for e in events if isinstance(e, Error)] == [], "the held-back Error never reached the caller"
    assert isinstance(events[-1], Done), "the turn ends with its one real terminal"


async def test_a_yielded_overflow_that_repeats_ends_the_turn_by_name_after_one_recovery() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(ContextOverflowUnrecoverable) as caught:
        await _invoke(executor)

    assert isinstance(caught.value.__cause__, BadRequestError) and GEMINI_OVERFLOW in caught.value.__cause__.message
    assert spy.forced == 1 and llm.calls == 2, "attempted once, not in a loop"


def test_the_classifier_still_recognises_the_failure_a_yielded_overflow_becomes() -> None:
    error = Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True)
    assert is_context_overflow(TurnStreamFailure(error, partial_messages=[], rounds_completed=0)) is True


async def test_a_yielded_error_that_is_not_an_overflow_is_left_alone() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="rate_limit", message=GEMINI_OVERFLOW, fatal=True), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(TurnStreamFailure):
        await _invoke(executor)

    assert spy.forced == 0 and executor.replaced == [] and llm.calls == 1


VLLM_OVERFLOW = (
    "'max_tokens' or 'max_completion_tokens' is too large: 4096. This model's maximum context length is "
    "32768 tokens and your request has 29000 input tokens (4096 > 32768 - 29000)."
)
VLLM_CAP_EXCEEDS_CONTEXT = (
    "'max_tokens' or 'max_completion_tokens' is too large: 100000. This model's maximum context length is "
    "8192 tokens and your request has 50 input tokens (100000 > 8192 - 50)."
)


async def test_vllms_normal_overflow_form_still_force_compacts_and_replays() -> None:
    """primer always sends max_output_tokens, so on a vLLM profile an overflow arrives as "'max_tokens' ... is
    too large" with the cap well inside the context length. The history is what must shrink, and compaction
    can fix it: a lexical output-cap veto here turns every such overflow into a failed turn."""
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(VLLM_OVERFLOW, status_code=400))
    executor = _Executor(llm, spy)

    events = await _invoke(executor)

    assert spy.forced == 1 and llm.calls == 2
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_vllms_cap_larger_than_the_context_does_not_force_compact() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(VLLM_CAP_EXCEEDS_CONTEXT, status_code=400), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(BadRequestError):
        await _invoke(executor)

    assert spy.forced == 0 and executor.replaced == [] and llm.calls == 1


# --- the configured output cap: when it alone does not fit the window, no history can ----------------------

# MODEL.context_length is 4096. Both rejections below say the PROMPT does not fit: one by the provider's code
# (which the message-level arithmetic never sees), one by a text the classifier takes for an input overflow
# (a proxy that rewrites the provider's words). Either way the agent's own cap fills the window, so the
# compaction could only destroy the history and the replay would be rejected the same way.
CAP_AT_THE_WINDOW = 4096
CAP_ABOVE_THE_WINDOW = 100_000
_REJECTIONS = {
    "provider-code": lambda: BadRequestError("Something went wrong", code="context_length_exceeded", status_code=400),
    "input-overflow-text": lambda: BadRequestError(OVERFLOW, status_code=400),
}


@pytest.mark.parametrize("cap", [CAP_AT_THE_WINDOW, CAP_ABOVE_THE_WINDOW], ids=["cap-equals-window", "cap-above-window"])
@pytest.mark.parametrize("rejection", list(_REJECTIONS))
async def test_a_cap_that_alone_fills_the_window_is_not_compacted_away(rejection: str, cap: int) -> None:
    spy = _SpyCompaction()
    rejected = _REJECTIONS[rejection]()
    llm = _FailsThenAnswers(raises=rejected)
    executor = _Executor(llm, spy, max_output_tokens=cap)

    with pytest.raises(ContextOverflowUnrecoverable) as failed:
        await _invoke(executor)

    assert spy.forced == 0, "no history, however short, fits beside the cap: summarising it away cannot help"
    assert executor.replaced == [], "the persisted history must be left alone"
    assert llm.calls == 1, "and the turn must not be replayed against the same rejection"
    assert failed.value.__cause__ is rejected
    assert (failed.value.forced_compaction, failed.value.replay_attempted) == (False, False)
    # It says WHY, in the words the operator can act on: the agent's cap and the model's window.
    assert f"max_output_tokens ({cap})" in failed.value.message
    assert f"context window ({MODEL.context_length})" in failed.value.message


@pytest.mark.parametrize("rejection", list(_REJECTIONS))
async def test_a_cap_below_the_window_still_force_compacts_and_replays(rejection: str) -> None:
    """The cap fits (1024 < 4096): the history is what does not, and compaction can fix it."""
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=_REJECTIONS[rejection]())
    executor = _Executor(llm, spy, max_output_tokens=1024)

    events = await _invoke(executor)

    assert spy.forced == 1 and len(executor.replaced) == 1 and llm.calls == 2
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_a_cap_one_below_the_window_still_force_compacts_and_replays() -> None:
    """The boundary: 4095 < 4096 fits (one token of history), 4096 does not (see the equal case above)."""
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=_REJECTIONS["provider-code"]())
    executor = _Executor(llm, spy, max_output_tokens=CAP_AT_THE_WINDOW - 1)

    await _invoke(executor)

    assert spy.forced == 1 and llm.calls == 2


async def test_an_unset_cap_proves_nothing_and_still_force_compacts_and_replays() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=_REJECTIONS["provider-code"]())
    executor = _Executor(llm, spy)  # no max_output_tokens: the adapter's own default applies

    await _invoke(executor)

    assert spy.forced == 1 and llm.calls == 2


async def test_the_recovery_is_attempted_once_not_in_a_loop() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(raises=BadRequestError(OVERFLOW, status_code=400), failures=99)
    executor = _Executor(llm, spy)

    # A rejected replay is not raised raw any more: it is the typed failure (the provider's rejection is its
    # __cause__), carrying how far recovery got.
    with pytest.raises(ContextOverflowUnrecoverable) as failed:
        await _invoke(executor)

    assert isinstance(failed.value.__cause__, BadRequestError)
    assert (failed.value.forced_compaction, failed.value.replay_attempted) == (True, True)
    assert spy.forced == 1 and llm.calls == 2


async def test_a_yielded_failure_that_is_not_an_overflow_still_propagates() -> None:
    spy = _SpyCompaction()
    llm = _FailsThenAnswers(yields=Error(code="rate_limit", message="slow down", fatal=True), failures=99)
    executor = _Executor(llm, spy)

    with pytest.raises(TurnStreamFailure):
        await _invoke(executor)

    assert spy.forced == 0 and llm.calls == 1
