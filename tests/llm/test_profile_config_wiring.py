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
    """Each test sees the "log once" record empty, whatever ran before it."""
    _reasoning._LOGGED_ONCE.clear()
    yield
    _reasoning._LOGGED_ONCE.clear()


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
