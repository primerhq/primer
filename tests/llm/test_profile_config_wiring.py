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

    @pytest.mark.parametrize("model, level, expected", [
        ("gpt-4o", ReasoningLevel.LOW, {}),  # does not reason: no effort at all
        ("gpt-4.1", ReasoningLevel.HIGH, {}),
        ("gpt-5-chat-latest", ReasoningLevel.OFF, {}),
        ("o3", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
        ("o4-mini", ReasoningLevel.MINIMAL, {"reasoning_effort": "low"}),
        ("gpt-5", ReasoningLevel.OFF, {"reasoning_effort": "minimal"}),
        ("gpt-5-mini", ReasoningLevel.HIGH, {"reasoning_effort": "high"}),
        ("gpt-5.1", ReasoningLevel.OFF, {"reasoning_effort": "none"}),
        ("gpt-5.1", ReasoningLevel.MINIMAL, {"reasoning_effort": "low"}),
        ("gpt-5.1-codex", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
        ("gpt-5-pro", ReasoningLevel.LOW, {"reasoning_effort": "high"}),
        ("gpt-5.2", ReasoningLevel.OFF, {"reasoning_effort": "none"}),
        # GPT-6: gpt-6-astra has no "none", gpt-6.1-sol neither "none" nor "minimal".
        ("gpt-6-astra", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
        ("gpt-6-astra", ReasoningLevel.MINIMAL, {"reasoning_effort": "low"}),
        ("gpt-6.1-sol", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
        ("gpt-6.1-sol", ReasoningLevel.MINIMAL, {"reasoning_effort": "low"}),
        ("gpt-6.1-sol", ReasoningLevel.HIGH, {"reasoning_effort": "high"}),
        # Not in the table: the generic value; the stream-open guard covers a refusal.
        ("my-deployment", ReasoningLevel.OFF, {"reasoning_effort": "minimal"}),
    ])
    def test_openchat_openai_effort_is_the_models(self, model, level, expected) -> None:
        llm = OpenChatLLM(_openchat(OpenChatFlavor.OPENAI))
        assert llm.profile_extended(_cfg(level), model=model) == expected

    @pytest.mark.parametrize("model, level, expected", [
        ("gpt-4o-mini", ReasoningLevel.HIGH, {}),
        ("o3-mini", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
        ("gpt-5.2", ReasoningLevel.OFF, {"reasoning_effort": "none"}),
        ("gpt-6.1-sol", ReasoningLevel.OFF, {"reasoning_effort": "low"}),
    ])
    def test_openresponses_effort_is_the_models(self, model, level, expected) -> None:
        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI))
        assert llm.profile_extended(_cfg(level), model=model) == expected

    def test_a_model_that_does_not_reason_drops_a_passthrough_effort_too(self, caplog) -> None:
        """The typed level wins, and the model takes no effort at all."""
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = OpenChatLLM(_openchat(OpenChatFlavor.OPENAI))
        cfg = _cfg(ReasoningLevel.LOW, reasoning_effort="high", seed=1)
        assert llm.profile_extended(cfg, model="gpt-4o") == {"seed": 1}
        assert any("does not reason" in r.getMessage() for r in caplog.records)

    def test_openresponses_vllm_maps_nothing_but_keeps_the_passthrough(self) -> None:
        """Verified against a live vLLM: its Responses endpoint honours no
        reasoning knob, so the mapping emits none (and warns)."""
        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.VLLM))
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF, seed=4)) == {"seed": 4}

    @pytest.mark.parametrize("model, level, expected", [
        # Claude 4.5 and earlier: a thinking budget, never effort.
        ("claude-haiku-4-5", ReasoningLevel.OFF, {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-4-5-20250929", ReasoningLevel.LOW,
         {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
        ("claude-opus-4-5", ReasoningLevel.HIGH,
         {"thinking": {"type": "enabled", "budget_tokens": 32768}}),
        ("claude-3-7-sonnet-20250219", ReasoningLevel.MINIMAL,
         {"thinking": {"type": "enabled", "budget_tokens": 1024}}),
        # Claude 4.6: adaptive and effort; thinking is off when omitted.
        ("claude-sonnet-4-6", ReasoningLevel.OFF, {}),
        ("claude-opus-4-6", ReasoningLevel.MINIMAL,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
        # Claude Opus 4.7 / 4.8, Opus 5, Sonnet 5, Haiku 5.5: disabled is accepted.
        ("claude-opus-4-7", ReasoningLevel.OFF, {"thinking": {"type": "disabled"}}),
        ("claude-opus-4-8", ReasoningLevel.MEDIUM,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}),
        ("claude-opus-5", ReasoningLevel.OFF, {"thinking": {"type": "disabled"}}),
        ("claude-opus-5", ReasoningLevel.HIGH,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
        ("claude-sonnet-5", ReasoningLevel.OFF, {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-5", ReasoningLevel.LOW,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
        ("claude-haiku-5-5", ReasoningLevel.OFF, {"thinking": {"type": "disabled"}}),
        ("claude-haiku-5-5", ReasoningLevel.MEDIUM,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}),
        # Claude Sonnet 5.5 refuses disabled: between_tools is its off.
        ("claude-sonnet-5-5", ReasoningLevel.OFF, {"thinking": {"type": "between_tools"}}),
        ("claude-sonnet-5-5", ReasoningLevel.HIGH,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
        # Claude Opus 5.5, Fable, Mythos: thinking cannot be turned off.
        ("claude-opus-5-5", ReasoningLevel.OFF, {"output_config": {"effort": "low"}}),
        ("claude-opus-5-5", ReasoningLevel.MEDIUM,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}),
        ("claude-fable-5-1", ReasoningLevel.OFF, {"output_config": {"effort": "low"}}),
        ("claude-mythos-5-1", ReasoningLevel.HIGH,
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
        # No thinking control at all: nothing is sent.
        ("claude-3-5-haiku-20241022", ReasoningLevel.HIGH, {}),
    ])
    def test_anthropic_takes_the_shape_of_the_model(self, model, level, expected) -> None:
        llm = AnthropicLLM(_anthropic())
        assert llm.profile_extended(_cfg(level), model=model) == expected

    @pytest.mark.parametrize("model", ["claude-opus-6", "my-proxy-model", None])
    def test_anthropic_sends_no_reasoning_to_a_model_it_does_not_know(self, model, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = AnthropicLLM(_anthropic())
        for _ in range(2):
            assert llm.profile_extended(_cfg(ReasoningLevel.HIGH, metadata={"user_id": "u"}), model=model) == {
                "metadata": {"user_id": "u"},
            }
        hits = [r for r in caplog.records if "not in the model table" in r.getMessage()]
        assert len(hits) == 1

    def test_anthropic_off_where_omitted_drops_a_passthrough_thinking(self) -> None:
        """The typed level wins over the same key in ``extended``."""
        llm = AnthropicLLM(_anthropic())
        cfg = _cfg(ReasoningLevel.OFF, thinking={"type": "adaptive"})
        assert llm.profile_extended(cfg, model="claude-sonnet-4-6") == {}

    def test_anthropic_effort_keeps_a_passthrough_output_config(self) -> None:
        llm = AnthropicLLM(_anthropic())
        cfg = _cfg(ReasoningLevel.LOW, output_config={"format": {"type": "json_schema"}})
        assert llm.profile_extended(cfg, model="claude-opus-4-7")["output_config"] == {
            "format": {"type": "json_schema"}, "effort": "low",
        }

    @pytest.mark.parametrize("model, level, expected", [
        # Gemini 2.5: a budget within the model's range.
        ("gemini-2.5-flash", ReasoningLevel.OFF, {"thinking_budget": 0}),
        ("gemini-2.5-flash", ReasoningLevel.HIGH, {"thinking_budget": 24576}),
        ("gemini-2.5-flash-lite", ReasoningLevel.OFF, {"thinking_budget": 0}),
        ("gemini-2.5-flash-lite-preview-09-2025", ReasoningLevel.MINIMAL, {"thinking_budget": 512}),
        ("gemini-2.5-pro", ReasoningLevel.OFF, {"thinking_budget": 128}),  # cannot turn off
        ("gemini-2.5-pro", ReasoningLevel.LOW, {"thinking_budget": 2048}),
        # Gemini 3 and later: a thinking level, OFF the lowest the model has.
        ("gemini-3-pro-preview", ReasoningLevel.OFF, {"thinking_level": "low"}),
        ("gemini-3-pro-preview", ReasoningLevel.HIGH, {"thinking_level": "high"}),
        ("gemini-3-flash-preview", ReasoningLevel.OFF, {"thinking_level": "minimal"}),
        ("gemini-3-flash-preview", ReasoningLevel.MEDIUM, {"thinking_level": "medium"}),
        ("models/gemini-3.1-pro-preview", ReasoningLevel.MINIMAL, {"thinking_level": "low"}),
        # Clamped to the levels each model documents (nearest, ties go higher).
        ("gemini-3.8-flash", ReasoningLevel.OFF, {"thinking_level": "low"}),  # no minimal
        ("gemini-3.7-flash", ReasoningLevel.MINIMAL, {"thinking_level": "low"}),
        ("gemini-3.6-flash", ReasoningLevel.OFF, {"thinking_level": "minimal"}),
        ("gemini-3-pro-preview", ReasoningLevel.MEDIUM, {"thinking_level": "high"}),  # low/high only
        ("gemini-3.1-pro-preview", ReasoningLevel.MEDIUM, {"thinking_level": "medium"}),
        ("gemini-3.1-flash-lite-image", ReasoningLevel.OFF, {"thinking_level": "minimal"}),
        ("gemini-3.1-flash-lite-image", ReasoningLevel.LOW, {"thinking_level": "minimal"}),
        ("gemini-3.1-flash-lite-image", ReasoningLevel.MEDIUM, {"thinking_level": "high"}),
        ("gemini-3-pro-image-preview", ReasoningLevel.OFF, {"thinking_level": "high"}),  # high only
        ("gemini-4-argon", ReasoningLevel.OFF, {"thinking_level": "low"}),
        ("gemini-4-argon", ReasoningLevel.MEDIUM, {"thinking_level": "medium"}),
        ("gemini-9-zeta", ReasoningLevel.OFF, {"thinking_level": "low"}),  # not in the table
    ])
    def test_gemini_takes_the_shape_of_the_model(self, model, level, expected) -> None:
        llm = GeminiLLM(_gemini())
        assert llm.profile_extended(_cfg(level), model=model) == expected

    def test_a_gemini_level_clamp_is_logged_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = GeminiLLM(_gemini())
        for _ in range(3):
            llm.profile_extended(_cfg(ReasoningLevel.MEDIUM), model="gemini-3-pro-preview")
        hits = [r for r in caplog.records if "has no thinking level 'medium'" in r.getMessage()]
        assert len(hits) == 1
        assert "'high'" in hits[0].getMessage()

    def test_gemini_2_5_pro_off_is_logged_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = GeminiLLM(_gemini())
        for _ in range(2):
            llm.profile_extended(_cfg(ReasoningLevel.OFF), model="gemini-2.5-pro")
        assert len([r for r in caplog.records if "cannot turn thinking off" in r.getMessage()]) == 1

    @pytest.mark.parametrize("model", ["gemini-2.0-flash", "gemma-3-27b-it", None])
    def test_gemini_sends_no_reasoning_to_a_model_without_the_control(self, model) -> None:
        llm = GeminiLLM(_gemini())
        assert llm.profile_extended(_cfg(ReasoningLevel.HIGH, seed=2), model=model) == {"seed": 2}

    def test_gemini_never_sends_a_budget_and_a_level(self) -> None:
        """The typed level wins over a passthrough of the other knob."""
        llm = GeminiLLM(_gemini())
        on_3 = llm.profile_extended(_cfg(ReasoningLevel.LOW, thinking_budget=99), model="gemini-3-pro-preview")
        on_25 = llm.profile_extended(_cfg(ReasoningLevel.LOW, thinking_level="high"), model="gemini-2.5-flash")
        assert on_3 == {"thinking_level": "low"}
        assert on_25 == {"thinking_budget": 2048}

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
        assert llm.profile_extended(_cfg(ReasoningLevel.OFF, top_k=20), model="claude-haiku-4-5") == {
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

    async def test_openchat_sends_no_effort_to_a_model_that_does_not_reason(self, monkeypatch) -> None:
        wire = _wire_for("openchat", monkeypatch)
        await _call_under(OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), _cfg(ReasoningLevel.LOW), model="gpt-4o")
        assert "reasoning_effort" not in _body(wire)

    async def test_openchat_retries_once_without_an_effort_the_model_refuses(self, monkeypatch) -> None:
        """A deployment name the table cannot classify gets the generic value;
        when the model refuses it, the call is made again without it."""
        http = _sdk_http_module()
        bodies: list[dict] = []

        def _handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            if "reasoning_effort" in body:
                return http.Response(400, json={"error": {
                    "message": "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
                    "type": "invalid_request_error", "param": "reasoning_effort",
                    "code": "unsupported_parameter",
                }})
            return http.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

        real = AsyncOpenAI
        monkeypatch.setattr(
            "primer.llm.openchat.AsyncOpenAI",
            lambda **kw: real(**kw, http_client=http.AsyncClient(transport=http.MockTransport(_handle))),
        )
        await _call_under(OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), _cfg(ReasoningLevel.LOW), model="my-deployment")
        assert [("reasoning_effort" in b) for b in bodies] == [True, False]

    async def test_openchat_vllm_422_on_the_template_switch_retries_without_it(self, monkeypatch) -> None:
        """vLLM validates the request body and answers 422 for a field it
        refuses; the classifier maps it to BadRequestError, so the guard
        makes the call once more without the profile's settings."""
        http = _sdk_http_module()
        bodies: list[dict] = []

        def _handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            if "chat_template_kwargs" in body:
                return http.Response(422, json={"detail": [{
                    "type": "extra_forbidden", "loc": ["body", "chat_template_kwargs"],
                    "msg": "Extra inputs are not permitted", "input": {"enable_thinking": False},
                }]})
            return http.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

        real = AsyncOpenAI
        monkeypatch.setattr(
            "primer.llm.openchat.AsyncOpenAI",
            lambda **kw: real(**kw, http_client=http.AsyncClient(transport=http.MockTransport(_handle))),
        )
        await _call_under(OpenChatLLM(_openchat(OpenChatFlavor.VLLM)), _cfg(ReasoningLevel.OFF), model="qwen3")
        assert [("chat_template_kwargs" in b) for b in bodies] == [True, False]

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

    async def test_openrouter_off_on_a_mandatory_reasoning_model_retries_without_it(
        self, monkeypatch, caplog,
    ) -> None:
        """OpenRouter refuses ``effort: "none"`` for a model whose reasoning
        is mandatory: the call is made once more without the reasoning
        object, logged once, and that model is not asked again."""
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        http = _sdk_http_module()
        bodies: list[dict] = []

        def _handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            if body.get("reasoning") == {"effort": "none"}:
                return http.Response(400, json={"error": {
                    "message": "Reasoning is mandatory for this endpoint and cannot be disabled.",
                    "code": 400,
                }})
            return http.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

        real = AsyncOpenAI
        monkeypatch.setattr(
            "primer.llm.openrouter.AsyncOpenAI",
            lambda **kw: real(**kw, http_client=http.AsyncClient(transport=http.MockTransport(_handle))),
        )
        llm = OpenRouterLLM(_openrouter())  # the registry hands out one cached adapter
        try:
            for _ in range(2):
                knobs = profile_extended(llm, _cfg(ReasoningLevel.OFF), model="openai/o3")
                async for _ev in profile_stream(llm, knobs, model="openai/o3", messages=_HI):
                    pass
        finally:
            await llm.aclose()
        assert [("reasoning" in b) for b in bodies] == [True, False, False]
        hits = [r for r in caplog.records if "mandatory" in r.getMessage()]
        assert len(hits) == 1

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

    @pytest.mark.parametrize("model, level, effort", [
        (None, ReasoningLevel.HIGH, "high"),
        ("gpt-5.1", ReasoningLevel.OFF, "none"),
    ])
    def test_openresponses_sends_reasoning_effort_as_reasoning(self, model, level, effort) -> None:
        from primer.llm.openresponses import _extract_extended_kwargs

        llm = OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI))
        assert _extract_extended_kwargs(llm.profile_extended(_cfg(level), model=model)) == {
            "reasoning": {"effort": effort},
        }

    @pytest.mark.parametrize("model, expected", [
        ("claude-haiku-4-5", {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
        ("claude-sonnet-4-6",
         {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
    ])
    def test_anthropic_sends_the_shape_of_the_model(self, model, expected) -> None:
        from primer.llm.anthropic import _extract_extended_kwargs

        llm = AnthropicLLM(_anthropic())
        knobs = llm.profile_extended(_cfg(ReasoningLevel.LOW), model=model)
        assert _extract_extended_kwargs(knobs) == expected

    def test_gemini_folds_the_budget_into_thinking_config(self) -> None:
        from primer.llm.gemini import _extract_extended_kwargs

        llm = GeminiLLM(_gemini())
        knobs = llm.profile_extended(_cfg(ReasoningLevel.OFF), model="gemini-2.5-flash")
        out = _extract_extended_kwargs(knobs)
        assert out["thinking_config"].thinking_budget == 0
        assert out["thinking_config"].thinking_level is None

    def test_gemini_folds_the_level_into_thinking_config(self) -> None:
        from primer.llm.gemini import _extract_extended_kwargs

        llm = GeminiLLM(_gemini())
        knobs = llm.profile_extended(_cfg(ReasoningLevel.LOW), model="gemini-3-pro-preview")
        config = _extract_extended_kwargs(knobs)["thinking_config"]
        assert config.thinking_level.value == "LOW"
        assert config.thinking_budget is None

    def test_gemini_with_both_knobs_keeps_the_level(self) -> None:
        from primer.llm.gemini import _extract_extended_kwargs

        config = _extract_extended_kwargs(
            {"thinking_budget": 512, "thinking_level": "high"},
        )["thinking_config"]
        assert config.thinking_level.value == "HIGH" and config.thinking_budget is None

    async def test_gemini_retries_once_without_a_setting_the_model_refuses(self, monkeypatch, caplog) -> None:
        """A model the table gets wrong answers 400: the call is made again
        without the profile's settings, and that is logged once."""
        from tests.llm.test_gemini import _aiter as _gaiter
        from tests.llm.test_gemini import _make_api_error, _ok_chunks
        from tests.llm.test_gemini import _patched_client as _gemini_client

        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        client = _gemini_client(monkeypatch)

        def _answer(**kwargs):
            if kwargs["config"].thinking_config is not None:
                raise _make_api_error(400, message="Budget 0 is invalid. This model only works in thinking mode.")
            return _gaiter(_ok_chunks())

        client.aio.models.generate_content_stream.side_effect = _answer
        llm = GeminiLLM(_gemini())
        knobs = llm.profile_extended(_cfg(ReasoningLevel.OFF), model="gemini-2.5-flash")
        events = [ev async for ev in profile_stream(llm, knobs, model="gemini-2.5-flash", messages=_HI)]
        assert any(type(e).__name__ == "TextDelta" for e in events)
        calls = client.aio.models.generate_content_stream.call_args_list
        assert len(calls) == 2
        assert calls[1].kwargs["config"].thinking_config is None
        assert any("thinking mode" in r.getMessage() for r in caplog.records)

    async def test_ollama_retries_once_without_think_on_a_model_that_cannot_think(
        self, monkeypatch, caplog,
    ) -> None:
        """Ollama answers ``think`` on a model without the capability with a
        400, which the lazily-opened adapter reports as its first event."""
        from tests.llm.test_ollama import _aiter as _oaiter
        from tests.llm.test_ollama import _make_response_error
        from tests.llm.test_ollama import _ok_chunks as _ollama_chunks
        from tests.llm.test_ollama import _patched_client as _ollama_client

        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        client = _ollama_client(monkeypatch)

        async def _refusing():
            raise _make_response_error(400, '"llama3.2" does not support thinking')
            yield  # pragma: no cover - makes this an async generator

        client.chat.side_effect = lambda **kw: _refusing() if "think" in kw else _oaiter(_ollama_chunks())
        llm = OllamaLLM(_ollama())
        for _ in range(2):
            knobs = llm.profile_extended(_cfg(ReasoningLevel.HIGH), model="llama3.2")
            events = [ev async for ev in profile_stream(llm, knobs, model="llama3.2", messages=_HI)]
            assert any(type(e).__name__ == "TextDelta" for e in events)
        assert [("think" in c.kwargs) for c in client.chat.call_args_list] == [True, False, False]
        assert len([r for r in caplog.records if "does not support thinking" in r.getMessage()]) == 1

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

from primer.llm.anthropic import _fit_sampling, _fit_thinking  # noqa: E402

_TOOL_ROUND = [
    Message(role="user", parts=[TextPart(text="go")]),
    Message(role="assistant", parts=[ToolCallPart(id="t1", name="x", arguments={})]),
    Message(role="tool", parts=[ToolResultPart(id="t1", output="done")]),
]


def _enabled(budget: int) -> dict:
    return {"type": "enabled", "budget_tokens": budget}


_ADAPTIVE = {"type": "adaptive"}


def _fit(req: dict, *, messages=_HI, model: str = "claude-sonnet-4-5", provider_id: str = "a") -> dict:
    """What the adapter's stream does: fit the thinking, then the sampling."""
    _fit_thinking(req, messages=messages, provider_id=provider_id)
    _fit_sampling(req, model=model, provider_id=provider_id)
    return req


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
        req = _fit({"max_tokens": 1024, "thinking": _enabled(1024), "temperature": 0.0})
        assert req == {"max_tokens": 1024, "temperature": 0.0}

    @pytest.mark.parametrize("thinking, model", [
        (_enabled(1024), "claude-sonnet-4-5"),
        (_ADAPTIVE, "claude-sonnet-4-6"),  # adaptive included
    ])
    def test_the_sampling_knobs_thinking_refuses_are_dropped(self, thinking, model) -> None:
        req = _fit({
            "max_tokens": 8192, "thinking": thinking,
            "temperature": 0.0, "top_k": 20, "top_p": 0.5,
        }, model=model)
        assert req == {"max_tokens": 8192, "thinking": thinking}

    def test_the_sampling_values_thinking_allows_are_kept(self) -> None:
        req = _fit({"max_tokens": 8192, "thinking": _enabled(1024), "temperature": 1, "top_p": 0.97})
        assert req["temperature"] == 1 and req["top_p"] == 0.97

    def test_without_thinking_sampling_is_kept_where_the_model_takes_it(self) -> None:
        req = _fit({"max_tokens": 8192, "temperature": 0.0, "top_k": 20}, model="claude-sonnet-4-6")
        assert req == {"max_tokens": 8192, "temperature": 0.0, "top_k": 20}

    def test_a_passthrough_top_k_is_dropped_where_the_model_takes_none(self) -> None:
        """Claude Opus 4.7 and later refuse sampling knobs, thinking or not."""
        req = _fit({"max_tokens": 8192, "top_k": 20}, model="claude-opus-4-7")
        assert req == {"max_tokens": 8192}

    @pytest.mark.parametrize("model", [
        "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5",
        "claude-sonnet-5", "claude-sonnet-5-5", "claude-haiku-5-5",
        "claude-fable-5-1", "claude-mythos-5-1",
    ])
    def test_every_call_drops_the_sampling_a_model_refuses(self, model) -> None:
        """Compaction asks for temperature 0 on every call, profile or not;
        these models answer any sampling knob (or any but its default) with
        a 400."""
        req = _fit({"max_tokens": 2048, "temperature": 0.0, "top_p": 0.9, "top_k": 5}, model=model)
        assert req == {"max_tokens": 2048}

    def test_an_unknown_model_keeps_its_sampling_and_says_so_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.anthropic")
        for _ in range(2):
            req = _fit({"max_tokens": 2048, "temperature": 0.0}, model="claude-opus-6", provider_id="ant-u")
            assert req == {"max_tokens": 2048, "temperature": 0.0}
        hits = [r for r in caplog.records if "not in the model table" in r.getMessage()]
        assert len(hits) == 1

    @pytest.mark.parametrize("model", [
        "claude-sonnet-4-6", "claude-opus-4-6", "claude-sonnet-4-5", "claude-haiku-4-5",
        "claude-opus-6",  # not in the table: left as it was
    ])
    def test_a_model_that_takes_sampling_keeps_it_without_thinking(self, model) -> None:
        req = _fit({"max_tokens": 2048, "temperature": 0.0, "top_p": 0.9}, model=model)
        assert req == {"max_tokens": 2048, "temperature": 0.0, "top_p": 0.9}

    @pytest.mark.parametrize("thinking", [_enabled(1024), _ADAPTIVE])
    @pytest.mark.parametrize("choice", [
        {"type": "any"}, {"type": "tool", "name": "structured_output"},
    ])
    def test_a_forced_tool_runs_without_thinking(self, choice, thinking) -> None:
        req = _fit({"max_tokens": 8192, "thinking": thinking, "tool_choice": choice, "temperature": 0.0},
                   model="claude-sonnet-4-6")
        assert "thinking" not in req
        assert req["temperature"] == 0.0

    def test_adaptive_thinking_continues_a_tool_round(self) -> None:
        """Adaptive thinking drops manual mode's thinking-block requirement."""
        req = _fit({"max_tokens": 8192, "thinking": _ADAPTIVE, "tool_choice": {"type": "auto"}},
                   messages=_TOOL_ROUND, model="claude-sonnet-4-6")
        assert req["thinking"] == _ADAPTIVE

    def test_adaptive_thinking_has_no_budget_to_fit(self) -> None:
        req = _fit({"max_tokens": 1024, "thinking": _ADAPTIVE}, model="claude-sonnet-4-6")
        assert req == {"max_tokens": 1024, "thinking": _ADAPTIVE}

    def test_a_tool_round_continuation_runs_without_thinking(self) -> None:
        """primer does not send thinking blocks back in the history, so the
        final assistant turn of a continuation cannot start with one."""
        req = {"max_tokens": 8192, "thinking": _enabled(1024), "tool_choice": {"type": "auto"}}
        _fit_thinking(req, messages=_TOOL_ROUND, provider_id="a")
        assert "thinking" not in req

    @pytest.mark.parametrize("thinking", [
        None, {"type": "disabled"}, {"type": "between_tools"},
    ])
    def test_no_thinking_and_thinking_off_are_left_alone(self, thinking) -> None:
        req = {"max_tokens": 1024, "temperature": 0.0, "top_k": 5}
        if thinking is not None:
            req["thinking"] = thinking
        before = dict(req)
        _fit(req, messages=_TOOL_ROUND, model="claude-sonnet-4-5")
        assert req == before

    def test_each_adjustment_is_logged_once(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.anthropic")
        for _ in range(3):
            _fit({"max_tokens": 4096, "thinking": _enabled(4096), "temperature": 0.0},
                 provider_id="ant-x")
        hits = [r for r in caplog.records if "ant-x" in r.getMessage()]
        assert len(hits) == 2  # the budget, the sampling knobs: once each

    async def _stream(self, monkeypatch, model: str, level: ReasoningLevel, **kwargs) -> dict:
        client = _patched_client(monkeypatch)
        client.messages.create.return_value = _aiter(_ok_events())
        llm = AnthropicLLM(_anthropic())
        knobs = profile_extended(llm, _cfg(level), model=model)
        async for _ in profile_stream(llm, knobs, model=model, messages=_HI, **kwargs):
            pass
        return client.messages.create.call_args.kwargs

    async def test_stream_fits_a_budget_model(self, monkeypatch) -> None:
        """Claude 4.5: the agent's default cap (4096) and compaction's
        temperature 0 with a LOW (4096-token) budget."""
        kwargs = await self._stream(monkeypatch, "claude-sonnet-4-5", ReasoningLevel.LOW, temperature=0.0)
        assert kwargs["thinking"] == _enabled(2048)
        assert kwargs["max_tokens"] == 4096
        assert "temperature" not in kwargs
        assert "output_config" not in kwargs

    async def test_stream_sends_adaptive_thinking_and_effort_on_4_6(self, monkeypatch) -> None:
        kwargs = await self._stream(monkeypatch, "claude-sonnet-4-6", ReasoningLevel.LOW, temperature=0.0)
        assert kwargs["thinking"] == _ADAPTIVE
        assert kwargs["output_config"] == {"effort": "low"}
        assert "temperature" not in kwargs

    async def test_stream_to_an_unknown_model_sends_no_reasoning(self, monkeypatch) -> None:
        kwargs = await self._stream(monkeypatch, "claude-opus-6", ReasoningLevel.HIGH, temperature=0.0)
        assert "thinking" not in kwargs and "output_config" not in kwargs
        assert kwargs["temperature"] == 0.0


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
        b = Member(translate=lambda c: anthropic_extended(c, model="claude-opus-4-7"))
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


# --------------------------------------------------------------------------- #
# OpenAI reasoning models get the request OpenAI accepts (audit G2-02/03/04)  #
# --------------------------------------------------------------------------- #

# A Responses stream the SDK parses: one completed event.
_RESPONSES_SSE = (
    b"event: response.completed\n"
    b'data: {"type":"response.completed","sequence_number":0,"response":{"id":"r","object":"response","created_at":1,'
    b'"model":"m","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2,'
    b'"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}}}\n\n'
)

# The call every case makes: the knobs compaction and an agent turn pass beside the profile's.
_CALL = {"temperature": 0.0, "top_p": 0.9, "max_output_tokens": 4096}


async def _whole_body(module: str, llm, cfg: ModelProfileConfig | None, model: str, monkeypatch) -> dict:
    """The JSON body the adapter put on the HTTP transport for one call under ``cfg``."""
    wire = _wire_for(module, monkeypatch)
    if module == "openresponses":
        wire.respond(200, content=_RESPONSES_SSE, headers={"content-type": "text/event-stream"})
    try:
        knobs = profile_extended(llm, cfg, model=model)
        async for _ in profile_stream(llm, knobs, model=model, messages=_HI, **_CALL):
            pass
    finally:
        await llm.aclose()
    assert len(wire.requests) == 1, "one request, no retry"
    return json.loads(wire.last.content)


def _chat_body(model: str, **rest) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True,
            "stream_options": {"include_usage": True}, **rest}


def _responses_body(model: str, **rest) -> dict:
    return {"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "store": False, "stream": True, **rest}


_SAMPLING = {"temperature": 0.0, "top_p": 0.9}


class TestOpenAIReasoningModelsGetTheRequestOpenAIAccepts:
    """Captured at the HTTP transport, whole. OpenAI's reasoning models refuse ``max_tokens`` on Chat Completions (they take
    ``max_completion_tokens``) and refuse ``temperature`` and ``top_p`` unless the effort is ``none``, with or without an
    effort sent (they reason by default). The fit is decided from the model's family and only on the ``openai`` flavor."""

    @pytest.mark.parametrize("model", ["o3", "gpt-5", "o4-mini"])
    @pytest.mark.parametrize("level, effort", [(ReasoningLevel.LOW, {"reasoning_effort": "low"}), (None, {})])
    async def test_chat_a_reasoning_family_takes_max_completion_tokens_and_no_sampling(self, monkeypatch, model, level, effort) -> None:
        cfg = None if level is None else _cfg(level)
        body = await _whole_body("openchat", OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), cfg, model, monkeypatch)
        assert body == _chat_body(model, max_completion_tokens=4096, **effort)

    async def test_chat_effort_none_keeps_the_sampling_knobs(self, monkeypatch) -> None:
        body = await _whole_body("openchat", OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), _cfg(ReasoningLevel.OFF), "gpt-5.1", monkeypatch)
        assert body == _chat_body("gpt-5.1", max_completion_tokens=4096, reasoning_effort="none", **_SAMPLING)

    @pytest.mark.parametrize("level", [ReasoningLevel.LOW, None])
    async def test_chat_a_model_that_does_not_reason_is_unchanged(self, monkeypatch, level) -> None:
        cfg = None if level is None else _cfg(level)
        body = await _whole_body("openchat", OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), cfg, "gpt-4o", monkeypatch)
        assert body == _chat_body("gpt-4o", max_tokens=4096, **_SAMPLING)

    async def test_chat_an_unknown_model_on_the_openai_flavor_is_unchanged(self, monkeypatch) -> None:
        """No family: ``max_tokens`` stays, and the sampling knobs stay unless an effort is sent (as before the family rule)."""
        llm = OpenChatLLM(_openchat(OpenChatFlavor.OPENAI))
        assert await _whole_body("openchat", llm, None, "my-deployment", monkeypatch) == _chat_body("my-deployment", max_tokens=4096, **_SAMPLING)
        llm = OpenChatLLM(_openchat(OpenChatFlavor.OPENAI))
        assert await _whole_body("openchat", llm, _cfg(ReasoningLevel.LOW), "my-deployment", monkeypatch) == _chat_body(
            "my-deployment", max_tokens=4096, reasoning_effort="low",
        )

    @pytest.mark.parametrize("flavor", [OpenChatFlavor.VLLM, OpenChatFlavor.OTHER])
    async def test_chat_an_openai_model_name_on_another_flavor_is_unchanged(self, monkeypatch, flavor) -> None:
        body = await _whole_body("openchat", OpenChatLLM(_openchat(flavor)), None, "o3", monkeypatch)
        assert body == _chat_body("o3", max_tokens=4096, **_SAMPLING)

    @pytest.mark.parametrize("model", ["o3", "gpt-5", "o4-mini"])
    @pytest.mark.parametrize("level, reasoning", [(ReasoningLevel.LOW, {"reasoning": {"effort": "low"}}), (None, {})])
    async def test_responses_a_reasoning_family_takes_no_sampling(self, monkeypatch, model, level, reasoning) -> None:
        cfg = None if level is None else _cfg(level)
        body = await _whole_body("openresponses", OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI)), cfg, model, monkeypatch)
        assert body == _responses_body(model, max_output_tokens=4096, **reasoning)

    @pytest.mark.parametrize("model", ["gpt-4o", "my-deployment"])
    async def test_responses_a_model_that_does_not_reason_or_is_unknown_is_unchanged(self, monkeypatch, model) -> None:
        body = await _whole_body("openresponses", OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OPENAI)), None, model, monkeypatch)
        assert body == _responses_body(model, max_output_tokens=4096, **_SAMPLING)

    async def test_responses_on_another_flavor_is_unchanged(self, monkeypatch) -> None:
        body = await _whole_body("openresponses", OpenResponsesLLM(_openresponses(OpenResponsesFlavor.OTHER)), None, "o3", monkeypatch)
        assert body == _responses_body("o3", max_output_tokens=4096, **_SAMPLING)

    async def test_the_change_is_logged_once(self, monkeypatch, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm.openchat")
        for _ in range(2):
            await _whole_body("openchat", OpenChatLLM(_openchat(OpenChatFlavor.OPENAI)), None, "o3", monkeypatch)
        hits = [r for r in caplog.records if "max_completion_tokens" in r.getMessage()]
        assert len(hits) == 1
        assert "temperature, top_p dropped" in hits[0].getMessage()
