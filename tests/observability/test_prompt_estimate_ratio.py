"""Phase 0 of the prompt-size accounting work: how far is our estimate from what the provider counted?

Every model call at the agent-loop seam that comes back with a provider ``Usage`` records the ratio
``usage.input_tokens / our estimate of the prompt that was sent`` on ``llm_prompt_estimate_ratio{provider_id}``, and
the estimate rides on the ``llm_call`` event as ``estimated_input_tokens``. Provider usage is free, so measuring costs no
counting; the histogram says whether the character heuristic the compaction trigger runs on is within a few percent of
the provider or off by 2x for some content, which decides whether a better estimator or a native count is worth
building. Nothing here changes a decision: it only records.

The estimate is the SAME figure the compaction trigger would have computed for that prompt (the per-part heuristic over
the system prompt, the history and the tool schemas), taken from the prompt as SENT (after any guard reduced it).
"""

from __future__ import annotations

import pytest

from primer.agent.loop import run_agent_turn
from primer.agent.tool_manager import ToolExecutionManager
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.agent import Agent
from primer.model.chat import (
    Done,
    ExtendedEvent,
    Message,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    _LlmCall,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


class _FakeLLM:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.sent: list[list[Message]] = []

    def stream(self, *, messages, **_kwargs):
        self.sent.append(list(messages))
        events = self._rounds.pop(0)

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


def _model(provider_id: str | None = "prov-1") -> ResolvedModel:
    return ResolvedModel(
        profile_id="prof-1", provider_id=provider_id, model_name="m-1", context_length=1000, config=ModelProfileConfig(),
    )


def _agent() -> Agent:
    return Agent(id="ag-1", description="d", model={"profile_id": "prof-1"})


PROMPT = [Message(role="user", parts=[TextPart(text="hi there, " * 40)])]


async def _drain(llm, *, model: ResolvedModel | None = None, prompt=PROMPT, budget=None, tool_manager=None) -> list:
    out = []
    async for ev in run_agent_turn(
        agent=_agent(), llm=llm, llm_model=model or _model(),
        tool_manager=tool_manager or ToolExecutionManager(toolset_providers={}, tools=[]),
        prompt=list(prompt), budget=budget,
    ):
        out.append(ev)
    return out


def _usage(n: int) -> Usage:
    return Usage(input_tokens=n, output_tokens=7, cumulative=False)


def _answer(usage: Usage | None) -> list:
    return [TextDelta(text="hello", index=0), *([usage] if usage else []), Done(stop_reason="stop", raw_reason="stop")]


def _samples(provider: str = "prov-1") -> tuple[float | None, float | None]:
    import primer.observability.metrics as m
    labels = {"provider_id": provider}
    return (
        m.registry.get_sample_value("llm_prompt_estimate_ratio_count", labels),
        m.registry.get_sample_value("llm_prompt_estimate_ratio_sum", labels),
    )


def _llm_calls(events: list) -> list[_LlmCall]:
    return [e.extended for e in events if isinstance(e, ExtendedEvent) and isinstance(e.extended, _LlmCall)]


class TestTheRatio:
    async def test_a_call_with_usage_records_usage_over_the_estimate_of_the_prompt_it_sent(self):
        estimate = count_tokens_char_fallback(messages=PROMPT)
        await _drain(_FakeLLM([_answer(_usage(2 * estimate))]))
        count, total = _samples()
        assert count == 1 and total == pytest.approx(2.0), "usage was twice our estimate"

    async def test_every_call_of_a_multi_round_turn_is_observed(self):
        tool_round = [
            ToolCallStart(id="c1", name="nope", index=0),
            ToolCallEnd(id="c1", arguments={}, index=0),
            _usage(100),
            Done(stop_reason="tool_use", raw_reason="tool_use"),
        ]
        llm = _FakeLLM([tool_round, _answer(_usage(150))])
        await _drain(llm)
        count, _ = _samples()
        assert count == 2, "one observation per model call, not per turn"
        assert len(llm.sent) == 2 and len(llm.sent[1]) > len(llm.sent[0]), "the second call carried the first round"

    async def test_a_call_with_no_usage_records_nothing_not_a_zero(self):
        import primer.observability.metrics as m

        await _drain(_FakeLLM([_answer(None)]))
        assert _samples() == (None, None), "no observation at all: not even the labelled child exists"
        assert m.registry.get_sample_value("llm_prompt_estimate_ratio_bucket", {"provider_id": "prov-1", "le": "+Inf"}) is None

    async def test_a_usage_of_zero_input_tokens_is_not_an_observation_either(self):
        await _drain(_FakeLLM([_answer(_usage(0))]))
        assert _samples() == (None, None)

    async def test_the_estimate_is_of_the_prompt_as_sent_after_a_guard_reduced_it(self):
        reduced = [Message(role="user", parts=[TextPart(text="short")])]

        class _Guard:
            async def before_call(self, prompt, *, tools):
                return reduced

            def after_call(self, usage):
                return None

        await _drain(_FakeLLM([_answer(_usage(100))]), budget=_Guard())
        count, total = _samples()
        assert count == 1 and total == pytest.approx(100 / count_tokens_char_fallback(messages=reduced))

    async def test_the_tool_schemas_are_part_of_the_estimate(self):
        from primer.model.chat import Tool

        tool = Tool(id="ts__t", toolset_id="ts", description="d" * 4_000, args_schema={"type": "object", "properties": {}})

        class _Manager(ToolExecutionManager):
            async def list_tools(self, *, principal=None):
                return [tool]

        await _drain(_FakeLLM([_answer(_usage(1_000))]), tool_manager=_Manager(toolset_providers={}, tools=[]))
        _, total = _samples()
        assert total == pytest.approx(1_000 / count_tokens_char_fallback(messages=PROMPT, tools=[tool]))

    async def test_the_label_is_the_provider_only_and_an_aggregated_profile_falls_back_to_its_profile(self):
        import primer.observability.metrics as m

        await _drain(_FakeLLM([_answer(_usage(100))]), model=_model(None))
        assert _samples("prof-1")[0] == 1, "no provider row: the profile id, as llm_calls_total does"
        assert m.llm_prompt_estimate_ratio._labelnames == ("provider_id",), "no model, no profile, nothing unbounded"

    def test_the_bucket_layout_is_pinned(self):
        """The buckets are what lets the histogram tell 0.9 from 1.1 and 1.5 from 2.0 (dense around 1.0, out to 0.25 and
        4.0 for a 2x error either way); prometheus's latency defaults (0.005..10) or a coarser tuple would record the same
        counts and lose that. The autouse fixture has just rebuilt the instrument through ``reset_for_test``, so this
        pins that definition (a module-level definition that drifts from it is the one ``primer`` imports: keep both)."""
        import primer.observability.metrics as m

        assert m.llm_prompt_estimate_ratio._upper_bounds[:-1] == [
            0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0, 4.0,
        ]

    async def test_a_prompt_with_nothing_to_estimate_records_nothing_and_does_not_divide_by_zero(self):
        """Usage reported for a prompt we estimate at 0 (no messages, no tools) has no ratio: the guard skips it instead of
        raising ZeroDivisionError out of the turn."""
        events = await _drain(_FakeLLM([_answer(_usage(50))]), prompt=[])
        assert _samples() == (None, None)
        (call,) = _llm_calls(events)
        assert call.input_tokens == 50 and call.estimated_input_tokens is None

    async def test_the_estimate_is_not_computed_for_a_call_that_has_no_usage(self, monkeypatch):
        """No usage means nothing to compare, so no pass over the prompt is spent."""
        import primer.agent.loop as loop

        calls: list[int] = []
        real = loop.count_tokens_char_fallback

        def counting(**kwargs):
            calls.append(1)
            return real(**kwargs)

        monkeypatch.setattr(loop, "count_tokens_char_fallback", counting)
        await _drain(_FakeLLM([_answer(None)]))
        assert calls == []
        await _drain(_FakeLLM([_answer(_usage(50))]))
        assert calls == [1]


class TestTheEventField:
    async def test_the_llm_call_event_carries_the_estimate_beside_the_reported_figure(self):
        events = await _drain(_FakeLLM([_answer(_usage(321))]))
        (call,) = _llm_calls(events)
        assert call.input_tokens == 321
        assert call.estimated_input_tokens == count_tokens_char_fallback(messages=PROMPT)

    async def test_without_usage_the_field_is_none(self):
        (call,) = _llm_calls(await _drain(_FakeLLM([_answer(None)])))
        assert call.input_tokens is None and call.estimated_input_tokens is None

    def test_the_record_carries_the_field_only_when_there_is_one(self):
        """A record for a call with no estimate is byte-identical to what it always was."""
        from primer.session.persistence import _CoalesceState, translate_stream_event

        def record(**extra):
            return translate_stream_event(ExtendedEvent(extended=_LlmCall(
                profile_id="p", provider_id="v", model="m", input_tokens=11, output_tokens=7, duration_ms=250,
                status="ok", **extra,
            )), _CoalesceState())

        assert "estimated_input_tokens" not in record().payload
        assert record(estimated_input_tokens=9).payload["estimated_input_tokens"] == 9
