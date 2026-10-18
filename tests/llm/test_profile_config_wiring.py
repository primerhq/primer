"""A model profile's ``config`` reaches each adapter's wire.

A profile carries ``config: {reasoning, extended}``. Every call made under it
passes that config to the adapter as ``stream(extended=...)``, translated for
the adapter's own wire by :mod:`primer.llm._reasoning`
(:meth:`primer.int.llm.LLM.profile_extended`). These tests pin, per provider
kind, that the adapter picks its own translation (and flavor). The call sites
are pinned in ``tests/agent/test_profile_config_call_sites.py`` and
``tests/graph/test_agent_node_profile_config.py``.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import HttpUrl, SecretStr

from primer.int.llm import LLM
from primer.llm import _reasoning
from primer.llm.anthropic import AnthropicLLM
from primer.llm.gemini import GeminiLLM
from primer.llm.ollama import OllamaLLM
from primer.llm.openchat import OpenChatLLM
from primer.llm.openresponses import OpenResponsesLLM
from primer.llm.openrouter import OpenRouterLLM
from primer.llm.retrying import RetryingLLM
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel
from primer.model.provider import (
    AnthropicConfig,
    GoogleConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
    OpenChatConfig,
    OpenChatFlavor,
    OpenResponsesConfig,
    OpenResponsesFlavor,
    OpenRouterConfig,
)


@pytest.fixture(autouse=True)
def _fresh_once_log():
    """Each test sees the "log once" record and the refusal memo empty."""
    _reasoning._LOGGED_ONCE.clear()
    _reasoning._REFUSED.clear()
    yield
    _reasoning._LOGGED_ONCE.clear()
    _reasoning._REFUSED.clear()


def _cfg(level: ReasoningLevel | None = None, **extended) -> ModelProfileConfig:
    return ModelProfileConfig(reasoning=level, extended=extended)


def _provider(kind: LLMProviderType, config) -> LLMProvider:
    return LLMProvider(
        id=f"{kind.value}-1", provider=kind, config=config,
        limits=Limits(max_concurrency=1),
    )


def _openchat(flavor: OpenChatFlavor = OpenChatFlavor.OPENAI) -> LLMProvider:
    return _provider(LLMProviderType.OPENCHAT, OpenChatConfig(
        url=HttpUrl("http://localhost:8000/v1/"), flavor=flavor,
        api_key=SecretStr("sk-test"),
    ))


def _openresponses(flavor: OpenResponsesFlavor = OpenResponsesFlavor.OPENAI) -> LLMProvider:
    return _provider(LLMProviderType.OPENRESPONSES, OpenResponsesConfig(
        url=HttpUrl("http://localhost:8000/v1/"), flavor=flavor,
        api_key=SecretStr("sk-test"),
    ))


def _anthropic() -> LLMProvider:
    return _provider(LLMProviderType.ANTHROPIC, AnthropicConfig(api_key=SecretStr("sk-ant")))


def _gemini() -> LLMProvider:
    return _provider(LLMProviderType.GEMINI, GoogleConfig(api_key=SecretStr("g-key")))


def _ollama() -> LLMProvider:
    return _provider(LLMProviderType.OLLAMA, OllamaConfig(url=HttpUrl("http://localhost:11434")))


def _openrouter() -> LLMProvider:
    return _provider(LLMProviderType.OPENROUTER, OpenRouterConfig(api_key=SecretStr("sk-or")))


class TestEachAdapterTranslatesForItsOwnWire:
    """``LLM.profile_extended`` is each adapter's own mapping from _reasoning."""

    def test_openchat_openai_takes_reasoning_effort(self) -> None:
        llm = OpenChatLLM(_openchat(OpenChatFlavor.OPENAI))
        assert llm.profile_extended(_cfg(ReasoningLevel.LOW)) == {"reasoning_effort": "low"}

    @pytest.mark.parametrize("flavor", [
        OpenChatFlavor.VLLM, OpenChatFlavor.OLLAMA, OpenChatFlavor.LMSTUDIO,
        OpenChatFlavor.OTHER,
    ])
    def test_openchat_self_hosted_flavors_take_the_template_switch(self, flavor) -> None:
        llm = OpenChatLLM(_openchat(flavor))
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF)) == {
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_openresponses_openai_takes_reasoning_effort(self) -> None:
        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI))
        assert llm.profile_extended(_cfg(ReasoningLevel.HIGH)) == {"reasoning_effort": "high"}

    def test_openresponses_vllm_maps_nothing_but_keeps_the_passthrough(self) -> None:
        """Verified against a live vLLM: its Responses endpoint honours no
        reasoning knob, so the mapping emits none (and warns)."""
        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.VLLM))
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF, seed=4)) == {"seed": 4}

    def test_anthropic_takes_a_thinking_block(self) -> None:
        llm = AnthropicLLM(_anthropic())
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF)) == {
            "thinking": {"type": "disabled"},
        }

    def test_gemini_takes_a_thinking_budget(self) -> None:
        llm = GeminiLLM(_gemini())
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF)) == {"thinking_budget": 0}

    def test_ollama_takes_a_think_flag(self) -> None:
        llm = OllamaLLM(_ollama())
        assert llm.profile_extended(_cfg(ReasoningLevel.MEDIUM)) == {"think": True}

    def test_openrouter_takes_its_reasoning_object(self) -> None:
        llm = OpenRouterLLM(_openrouter())
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF)) == {
            "reasoning": {"effort": "none"},
        }

    def test_the_retry_wrapper_translates_for_the_adapter_it_wraps(self) -> None:
        """The registry wraps every adapter in RetryingLLM; the ABC default on
        the wrapper must not shadow the inner adapter's own mapping."""
        provider = _openchat(OpenChatFlavor.VLLM)
        llm = RetryingLLM(OpenChatLLM(provider), provider)
        assert llm.profile_extended(_cfg(ReasoningLevel.HIGH)) == {
            "chat_template_kwargs": {"enable_thinking": True},
        }

    def test_the_passthrough_rides_along_with_the_level(self) -> None:
        llm = AnthropicLLM(_anthropic())
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF, top_k=20)) == {
            "top_k": 20, "thinking": {"type": "disabled"},
        }


class _Unmapped(LLM):
    """An adapter that never learned a reasoning mapping."""

    async def stream(self, **kwargs):  # pragma: no cover - never streamed here
        yield None

    async def count_tokens(self, *, model, messages, tools=None) -> int:  # pragma: no cover
        return 0


class TestAnAdapterWithoutAMapping:
    def test_forwards_extended_and_logs_the_level_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = _Unmapped()
        for _ in range(2):
            assert llm.profile_extended(_cfg(ReasoningLevel.HIGH, seed=1)) == {"seed": 1}
        hits = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "_Unmapped" in r.getMessage()
        ]
        assert len(hits) == 1
        assert "high" in hits[0].getMessage()

    def test_does_not_log_when_no_level_is_set(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        assert _Unmapped().profile_extended(_cfg(None, seed=1)) == {"seed": 1}
        assert not [r for r in caplog.records if "_Unmapped" in r.getMessage()]


# --------------------------------------------------------------------------- #
# The translated knobs reach the wire                                         #
# --------------------------------------------------------------------------- #

import json  # noqa: E402

from openai import AsyncOpenAI  # noqa: E402

from primer.llm._reasoning import profile_extended, profile_stream  # noqa: E402
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart  # noqa: E402
from tests.llm.test_anthropic import _aiter, _ok_events, _patched_client  # noqa: E402
from tests.llm.test_openrouter import _sdk_http_module, _Wire  # noqa: E402

_SSE = (
    b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
    b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)

_HI = [Message(role="user", parts=[TextPart(text="hi")])]


def _wire_for(module: str, monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """Route ``primer.llm.<module>``'s openai client through an in-process
    transport, so the test reads the JSON body the adapter really sends."""
    http = _sdk_http_module()
    wire = _Wire(http)
    real = AsyncOpenAI

    def _factory(**kwargs):
        return real(
            **kwargs,
            http_client=http.AsyncClient(transport=http.MockTransport(wire.handle)),
        )

    monkeypatch.setattr(f"primer.llm.{module}.AsyncOpenAI", _factory)
    wire.respond(200, content=_SSE, headers={"content-type": "text/event-stream"})
    return wire


async def _call_under(llm, cfg: ModelProfileConfig | None, *, model: str = "m") -> None:
    try:
        knobs = profile_extended(llm, cfg, model=model)
        async for _ in profile_stream(llm, knobs, model=model, messages=_HI):
            pass
    finally:
        await llm.aclose()


def _body(wire: _Wire) -> dict:
    return json.loads(wire.last.content)


class TestTheKnobsReachTheWire:
    """The openai SDK refuses a keyword it does not know, so a knob that is
    not one of its parameters must travel in ``extra_body``; these read the
    request body the adapter actually sends."""

    async def test_openchat_openai_sends_reasoning_effort(self, monkeypatch) -> None:
        wire = _wire_for("openchat", monkeypatch)
        await _call_under(OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), _cfg(ReasoningLevel.LOW, seed=7))
        body = _body(wire)
        assert body["reasoning_effort"] == "low"
        assert body["seed"] == 7

    async def test_openchat_vllm_sends_the_template_switch_in_the_body(self, monkeypatch) -> None:
        wire = _wire_for("openchat", monkeypatch)
        await _call_under(OpenChatLLM(_openchat(OpenChatFlavor.VLLM)), _cfg(ReasoningLevel.OFF))
        body = _body(wire)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert "reasoning_effort" not in body

    async def test_openrouter_sends_its_reasoning_object_in_the_body(self, monkeypatch) -> None:
        wire = _wire_for("openrouter", monkeypatch)
        await _call_under(OpenRouterLLM(_openrouter()), _cfg(ReasoningLevel.OFF))
        assert _body(wire)["reasoning"] == {"effort": "none"}

    @pytest.mark.parametrize("module, make", [
        ("openchat", lambda: OpenChatLLM(_openchat(OpenChatFlavor.VLLM))),
        ("openrouter", lambda: OpenRouterLLM(_openrouter())),
    ])
    async def test_no_config_sends_none_of_it(self, monkeypatch, module, make) -> None:
        wire = _wire_for(module, monkeypatch)
        await _call_under(make(), None)
        body = _body(wire)
        for key in ("reasoning_effort", "chat_template_kwargs", "reasoning", "extra_body"):
            assert key not in body


class TestEveryKindAcceptsItsTranslation:
    """The translation lands in each adapter's request, not in its drop list."""

    def test_openresponses_sends_reasoning_effort_as_reasoning(self) -> None:
        from primer.llm.openresponses import _extract_extended_kwargs

        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI))
        assert _extract_extended_kwargs(llm.profile_extended(_cfg(ReasoningLevel.HIGH))) == {
            "reasoning": {"effort": "high"},
        }

    def test_anthropic_sends_the_thinking_block(self) -> None:
        from primer.llm.anthropic import _extract_extended_kwargs

        llm = AnthropicLLM(_anthropic())
        assert _extract_extended_kwargs(llm.profile_extended(_cfg(ReasoningLevel.LOW))) == {
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }

    def test_gemini_folds_the_budget_into_thinking_config(self) -> None:
        from primer.llm.gemini import _extract_extended_kwargs

        llm = GeminiLLM(_gemini())
        out = _extract_extended_kwargs(llm.profile_extended(_cfg(ReasoningLevel.OFF)))
        assert out["thinking_config"].thinking_budget == 0

    def test_ollama_sends_think_at_the_top_level(self) -> None:
        from primer.llm.ollama import _build_options_and_kwargs

        llm = OllamaLLM(_ollama())
        _options, top_level = _build_options_and_kwargs(
            temperature=None, top_p=None, max_output_tokens=None, stop=None,
            extended=llm.profile_extended(_cfg(ReasoningLevel.OFF)),
        )
        assert top_level == {"think": False}


# --------------------------------------------------------------------------- #
# Anthropic: a thinking request the Messages API accepts                      #
# --------------------------------------------------------------------------- #

from primer.llm.anthropic import _fit_thinking  # noqa: E402

_TOOL_ROUND = [
    Message(role="user", parts=[TextPart(text="go")]),
    Message(role="assistant", parts=[ToolCallPart(id="t1", name="x", arguments={})]),
    Message(role="tool", parts=[ToolResultPart(id="t1", output="done")]),
]


def _enabled(budget: int) -> dict:
    return {"type": "enabled", "budget_tokens": budget}


class TestAnthropicThinkingFitsTheRequest:
    """Manual thinking needs a budget of at least 1024 below max_tokens, no
    temperature other than 1, no top_k, top_p of at least 0.95, no forced
    tool, and a final assistant turn that starts with a thinking block. A
    profile's level must not turn an otherwise valid call into a 400."""

    def test_a_budget_that_fits_is_left_alone(self) -> None:
        req = {"max_tokens": 8192, "thinking": _enabled(4096)}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req == {"max_tokens": 8192, "thinking": _enabled(4096)}

    def test_a_budget_not_below_max_tokens_is_lowered_to_half(self) -> None:
        req = {"max_tokens": 4096, "thinking": _enabled(4096)}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req["thinking"] == _enabled(2048)

    def test_a_small_cap_keeps_the_minimum_budget(self) -> None:
        req = {"max_tokens": 1500, "thinking": _enabled(4096)}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req["thinking"] == _enabled(1024)

    def test_no_room_for_the_minimum_budget_turns_thinking_off(self) -> None:
        req = {"max_tokens": 1024, "thinking": _enabled(1024), "temperature": 0.0}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req == {"max_tokens": 1024, "temperature": 0.0}

    def test_the_sampling_knobs_thinking_refuses_are_dropped(self) -> None:
        req = {
            "max_tokens": 8192, "thinking": _enabled(1024),
            "temperature": 0.0, "top_k": 20, "top_p": 0.5,
        }
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req == {"max_tokens": 8192, "thinking": _enabled(1024)}

    def test_the_sampling_values_thinking_allows_are_kept(self) -> None:
        req = {"max_tokens": 8192, "thinking": _enabled(1024), "temperature": 1, "top_p": 0.97}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert req["temperature"] == 1 and req["top_p"] == 0.97

    @pytest.mark.parametrize("choice", [
        {"type": "any"}, {"type": "tool", "name": "structured_output"},
    ])
    def test_a_forced_tool_runs_without_thinking(self, choice) -> None:
        req = {"max_tokens": 8192, "thinking": _enabled(1024), "tool_choice": choice, "temperature": 0.0}
        _fit_thinking(req, messages=_HI, provider_id="a")
        assert "thinking" not in req
        assert req["temperature"] == 0.0

    def test_a_tool_round_continuation_runs_without_thinking(self) -> None:
        """primer does not send thinking blocks back in the history, so the
        final assistant turn of a continuation cannot start with one."""
        req = {"max_tokens": 8192, "thinking": _enabled(1024), "tool_choice": {"type": "auto"}}
        _fit_thinking(req, messages=_TOOL_ROUND, provider_id="a")
        assert "thinking" not in req

    @pytest.mark.parametrize("thinking", [None, {"type": "disabled"}])
    def test_no_thinking_and_disabled_thinking_are_left_alone(self, thinking) -> None:
        req = {"max_tokens": 1024, "temperature": 0.0, "top_k": 5}
        if thinking is not None:
            req["thinking"] = thinking
        before = dict(req)
        _fit_thinking(req, messages=_TOOL_ROUND, provider_id="a")
        assert req == before

    def test_each_adjustment_is_logged_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.anthropic")
        for _ in range(3):
            _fit_thinking(
                {"max_tokens": 4096, "thinking": _enabled(4096), "temperature": 0.0},
                messages=_HI, provider_id="ant-x",
            )
        hits = [r for r in caplog.records if "ant-x" in r.getMessage()]
        assert len(hits) == 2  # the budget, the sampling knobs: once each

    async def test_stream_applies_the_fit_to_the_profile_thinking(self, monkeypatch) -> None:
        """End to end through the adapter: the agent's default cap (4096) and
        compaction's temperature 0 with a LOW (4096-token) budget."""
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        llm = AnthropicLLM(_anthropic())
        knobs = profile_extended(llm, _cfg(ReasoningLevel.LOW), model="claude-sonnet-4-5")
        async for _ in profile_stream(
            llm, knobs, model="claude-sonnet-4-5", messages=_HI, temperature=0.0,
        ):
            pass
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == _enabled(2048)
        assert kwargs["max_tokens"] == 4096
        assert "temperature" not in kwargs


# --------------------------------------------------------------------------- #
# An aggregated profile: each member applies its own config                   #
# --------------------------------------------------------------------------- #

from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

from primer.llm._reasoning import anthropic_extended, ollama_extended  # noqa: E402
from primer.llm.aggregated import AggregatedLLM  # noqa: E402
from primer.model.chat import Done, TextDelta  # noqa: E402
from primer.model.except_ import RateLimitError  # noqa: E402
from primer.model.model_profile import ModelProfile  # noqa: E402
from primer.model_profile.resolver import ResolvedModel  # noqa: E402

_ANSWER = [TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]


class Member(LLM):
    """A fake member adapter of one kind: translates with that kind's mapping,
    records every stream call's keyword arguments, and can fail at connect."""

    def __init__(
        self,
        *,
        translate: Callable[[ModelProfileConfig], dict[str, Any]] = ollama_extended,
        connect_exc: Exception | None = None,
    ) -> None:
        self._translate = translate
        self._connect_exc = connect_exc
        self.calls: list[dict[str, Any]] = []

    def profile_extended(
        self, config: ModelProfileConfig, *, model: str | None = None,
    ) -> dict[str, Any]:
        return self._translate(config)

    async def stream(self, *, model, messages, **kwargs):  # type: ignore[override]
        self.calls.append({"model": model, **kwargs})
        if self._connect_exc is not None:
            raise self._connect_exc
        for ev in _ANSWER:
            yield ev

    async def count_tokens(self, *, model, messages, tools=None) -> int:  # pragma: no cover
        return 1


def _aggregate(members: list[tuple[str, Member, ModelProfileConfig]], *, own: ModelProfileConfig | None = None):
    profile = ModelProfile(
        id="agg-cfg", description="pool", kind="aggregated",
        members=[m[0] for m in members], config=own or ModelProfileConfig(),
    )
    table = {mid: (llm, cfg) for mid, llm, cfg in members}

    async def resolve(member_id: str):
        llm, cfg = table[member_id]
        return llm, ResolvedModel(
            profile_id=member_id, provider_id=f"prov-{member_id}",
            model_name=f"model-{member_id}", context_length=8192, config=cfg,
        )

    return AggregatedLLM(profile, resolve_member=resolve)


async def _drain_agg(llm: AggregatedLLM, **kwargs) -> None:
    async for _ in llm.stream(model="virtual", messages=_HI, **kwargs):
        pass


class TestAnAggregatedProfile:
    async def test_each_member_applies_its_own_config_when_it_serves(self) -> None:
        """Member a fails at connect, member b serves: each call carried its
        own member's config, translated for that member's kind."""
        a = Member(translate=ollama_extended, connect_exc=RateLimitError("busy"))
        b = Member(translate=anthropic_extended)
        llm = _aggregate([
            ("m-a", a, _cfg(ReasoningLevel.HIGH, seed=1)),
            ("m-b", b, _cfg(ReasoningLevel.OFF)),
        ])
        await _drain_agg(llm)
        assert a.calls[0]["extended"] == {"seed": 1, "think": True}
        assert b.calls[0]["extended"] == {"thinking": {"type": "disabled"}}
        assert b.calls[0]["model"] == "model-m-b"

    async def test_the_aggregate_own_config_is_not_sent_to_a_member(self) -> None:
        b = Member()
        llm = _aggregate([("m-b", b, _cfg(ReasoningLevel.OFF))], own=_cfg(ReasoningLevel.HIGH))
        knobs = profile_extended(llm, llm._profile.config, model="virtual")
        await _drain_agg(llm, **({"extended": knobs} if knobs else {}))
        assert b.calls[0]["extended"] == {"think": False}

    async def test_a_member_with_no_config_is_called_exactly_as_before(self) -> None:
        b = Member()
        llm = _aggregate([("m-b", b, ModelProfileConfig())])
        await _drain_agg(llm)
        assert b.calls[0]["extended"] is None

    async def test_a_per_call_extended_rides_on_top_of_the_member_config(self) -> None:
        b = Member()
        llm = _aggregate([("m-b", b, _cfg(ReasoningLevel.HIGH, seed=1))])
        await _drain_agg(llm, extended={"seed": 9})
        assert b.calls[0]["extended"] == {"seed": 9, "think": True}

    def test_its_own_config_translates_to_nothing_and_is_logged_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = _aggregate([("m-b", Member(), ModelProfileConfig())])
        for _ in range(2):
            assert llm.profile_extended(_cfg(ReasoningLevel.LOW, seed=2)) == {}
        hits = [r for r in caplog.records if "agg-cfg" in r.getMessage()]
        assert len(hits) == 1
