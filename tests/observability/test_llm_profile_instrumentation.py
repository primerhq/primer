"""S7 section 4: per-profile LLM call + token counters at the loop seam.

The pre-existing llm_tokens_total keeps its provider-kind view; the gap
the terrain names is the per-PROFILE dimension (14-s7-terrain.md).
"""

from __future__ import annotations

import pytest

from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent
from primer.model.chat import Done, Message, TextDelta, TextPart, Usage
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from primer.agent.tool_manager import ToolExecutionManager


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


class _FakeLLM:
    def __init__(self, rounds):
        self._rounds = list(rounds)

    def stream(self, **_kwargs):
        events = self._rounds.pop(0)

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


def _model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="prof-1",
        provider_id="prov-1",
        model_name="m-1",
        context_length=1000,
        config=ModelProfileConfig(),
    )


def _agent() -> Agent:
    return Agent(id="ag-1", description="d", model={"profile_id": "prof-1"})


async def _drain(llm) -> list:
    out = []
    async for ev in run_agent_turn(
        agent=_agent(),
        llm=llm,
        llm_model=_model(),
        tool_manager=ToolExecutionManager(toolset_providers={}, tools=[]),
        prompt=[Message(role="user", parts=[TextPart(text="hi")])],
    ):
        out.append(ev)
    return out


async def test_one_call_counts_one_ok():
    import primer.observability.metrics as m
    await _drain(_FakeLLM([[
        TextDelta(text="hello", index=0),
        Usage(input_tokens=11, output_tokens=7, cumulative=False),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]))
    assert m.llm_calls_total.labels("prov-1", "prof-1", "ok")._value.get() == 1.0


async def test_tokens_split_by_direction():
    import primer.observability.metrics as m
    await _drain(_FakeLLM([[
        Usage(input_tokens=11, output_tokens=7, cumulative=False),
        Done(stop_reason="stop", raw_reason="stop"),
    ]]))
    assert m.llm_profile_tokens_total.labels("prof-1", "in")._value.get() == 11.0
    assert m.llm_profile_tokens_total.labels("prof-1", "out")._value.get() == 7.0


async def test_stream_failure_counts_error_and_propagates():
    import primer.observability.metrics as m

    class _Boom:
        def stream(self, **_kwargs):
            async def _gen():
                raise RuntimeError("upstream down")
                yield  # pragma: no cover

            return _gen()

    with pytest.raises(RuntimeError):
        await _drain(_Boom())
    assert m.llm_calls_total.labels("prov-1", "prof-1", "error")._value.get() == 1.0
