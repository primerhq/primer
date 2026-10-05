"""Phase 0b: what the ``llm_call`` record carries so the Phase 0 decision rule can be applied to records alone.

The Phase 0 histogram has a ``provider_id`` label only, so it mixes tokenizers behind one provider and the members of an
aggregated profile. The decision rule (docs/superpowers specs, native-token-counting v3.4, section 8) groups ``llm_call``
records by ``(provider_id, model)`` and needs, per call: the provider's figure, our estimate, the model's context window
(to recompute the trigger without joining a profile that may have changed), whether a prompt guard (a replay) was
involved, and the cached share the provider reported. Nothing here changes a decision: it only records.

A record for a call that reported no usage, and a call without a guard, is byte-identical to what it was.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.llm._openai_compat import _build_usage
from primer.model.agent import Agent
from primer.model.chat import Done, ExtendedEvent, Message, TextDelta, TextPart, Usage, _LlmCall
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from primer.session.persistence import _CoalesceState, translate_stream_event

PROMPT = [Message(role="user", parts=[TextPart(text="hi there, " * 40)])]


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


class _FakeLLM:
    def __init__(self, events):
        self._events = list(events)

    def stream(self, *, messages, **_kwargs):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="prof-1", provider_id="prov-1", model_name="m-1", context_length=4321, config=ModelProfileConfig(),
    )


async def _call(events, *, budget=None) -> _LlmCall:
    out = []
    async for ev in run_agent_turn(
        agent=Agent(id="ag-1", description="d", model={"profile_id": "prof-1"}), llm=_FakeLLM(events), llm_model=_model(),
        tool_manager=ToolExecutionManager(toolset_providers={}, tools=[]), prompt=list(PROMPT), budget=budget,
    ):
        if isinstance(ev, ExtendedEvent) and isinstance(ev.extended, _LlmCall):
            out.append(ev.extended)
    (call,) = out
    return call


def _answer(usage: Usage | None) -> list:
    return [TextDelta(text="hello", index=0), *([usage] if usage else []), Done(stop_reason="stop", raw_reason="stop")]


class _Guard:
    """A guard that returns what ``reduce`` makes of the prompt."""

    def __init__(self, reduce):
        self._reduce = reduce

    async def before_call(self, prompt, *, tools):
        return self._reduce(prompt)

    def after_call(self, usage):
        return None


class TestTheEvent:
    async def test_a_call_with_usage_carries_the_context_window_and_the_cached_share(self):
        call = await _call(_answer(Usage(input_tokens=900, output_tokens=7, cached_input_tokens=600, cumulative=False)))
        assert (call.input_tokens, call.cached_input_tokens, call.context_length) == (900, 600, 4321)
        assert call.estimated_input_tokens is not None
        assert call.guard == "none"

    async def test_a_call_without_usage_carries_none_of_them(self):
        call = await _call(_answer(None))
        assert (call.cached_input_tokens, call.context_length, call.estimated_input_tokens) == (None, None, None)

    async def test_a_usage_without_a_cached_figure_has_none(self):
        call = await _call(_answer(Usage(input_tokens=900, output_tokens=7, cumulative=False)))
        assert call.cached_input_tokens is None and call.context_length == 4321

    async def test_a_guard_that_sends_the_prompt_unchanged_is_kept(self):
        assert (await _call(_answer(None), budget=_Guard(lambda p: p))).guard == "kept"

    async def test_a_guard_that_returns_a_new_list_of_the_same_messages_is_still_kept(self):
        """``ReplayGuard`` returns a new list whose unreduced messages keep their identity."""
        assert (await _call(_answer(None), budget=_Guard(lambda p: list(p)))).guard == "kept"

    async def test_a_guard_that_reduces_a_message_or_drops_one_is_reduced(self):
        shorter = lambda p: [Message(role="user", parts=[TextPart(text="short")])]  # noqa: E731
        copied = lambda p: [m.model_copy() for m in p]  # noqa: E731 - same content, but not the same object: reduced
        assert (await _call(_answer(None), budget=_Guard(shorter))).guard == "reduced"
        assert (await _call(_answer(None), budget=_Guard(copied))).guard == "reduced"
        assert (await _call(_answer(None), budget=_Guard(lambda p: []))).guard == "reduced"


class TestTheRecord:
    @staticmethod
    def _record(**extra):
        return translate_stream_event(ExtendedEvent(extended=_LlmCall(
            profile_id="p", provider_id="v", model="m", input_tokens=11, output_tokens=7, duration_ms=250, status="ok",
            **extra,
        )), _CoalesceState())

    def test_a_record_without_the_new_fields_is_byte_identical_to_what_it_was(self):
        assert self._record().payload == {
            "profile_id": "p", "provider_id": "v", "model": "m", "input_tokens": 11, "output_tokens": 7,
            "duration_ms": 250, "status": "ok",
        }

    def test_the_record_carries_each_field_only_when_there_is_one(self):
        payload = self._record(cached_input_tokens=5, context_length=4321, guard="reduced").payload
        assert (payload["cached_input_tokens"], payload["context_length"], payload["guard"]) == (5, 4321, "reduced")
        assert "guard" not in self._record(guard="none").payload
        assert "cached_input_tokens" not in self._record(context_length=4321).payload
        assert self._record(guard="kept").payload["guard"] == "kept"


class TestOpenAiCompatCachedTokens:
    """``prompt_tokens`` includes the cached tokens and ``prompt_tokens_details.cached_tokens`` is a subset of it."""

    def test_the_cached_subset_is_reported(self):
        usage = _build_usage(SimpleNamespace(
            prompt_tokens=1_000, completion_tokens=40, prompt_tokens_details=SimpleNamespace(cached_tokens=600),
        ))
        assert (usage.input_tokens, usage.cached_input_tokens) == (1_000, 600)

    def test_a_server_that_sends_no_details_has_none(self):
        assert _build_usage(SimpleNamespace(prompt_tokens=1_000, completion_tokens=40)).cached_input_tokens is None
        assert _build_usage(SimpleNamespace(
            prompt_tokens=1_000, completion_tokens=40, prompt_tokens_details=None,
        )).cached_input_tokens is None
        assert _build_usage(SimpleNamespace(
            prompt_tokens=1_000, completion_tokens=40, prompt_tokens_details=SimpleNamespace(cached_tokens=None),
        )).cached_input_tokens is None

    def test_a_figure_that_cannot_be_a_subset_is_not_reported(self):
        """More cached than the whole prompt (or negative) is a server that means something else: not recorded."""
        for cached in (1_001, -1):
            usage = _build_usage(SimpleNamespace(
                prompt_tokens=1_000, completion_tokens=40, prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
            ))
            assert usage.cached_input_tokens is None
