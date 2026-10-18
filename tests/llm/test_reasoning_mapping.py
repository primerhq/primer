"""Per-vendor mapping of the neutral ReasoningLevel.

No two vendors expose the same control, so the profile carries a universal
level and each adapter translates. Where a vendor has no true "off" the
closest setting is used; where it has no working control at all the mapping
emits nothing and warns, because silently accepting a knob that does
nothing is how an operator ends up believing they disabled reasoning when
they did not.
"""

from __future__ import annotations

import logging

import pytest

from primer.llm import _reasoning
from primer.llm._reasoning import (
    anthropic_extended,
    gemini_extended,
    ollama_extended,
    openchat_extended,
    openresponses_extended,
    openrouter_extended,
    profile_extended,
    profile_stream_kwargs,
)
from primer.model.model_profile import ModelProfileConfig, ReasoningLevel


@pytest.fixture(autouse=True)
def _fresh_once_log():
    """Each test sees the "log once" record empty, whatever ran before it."""
    _reasoning._LOGGED_ONCE.clear()
    yield
    _reasoning._LOGGED_ONCE.clear()


def _cfg(level=None, **extended):
    return ModelProfileConfig(reasoning=level, extended=extended)


class TestNoLevelIsInert:
    """An unset level must leave the call byte-identical to before."""

    @pytest.mark.parametrize("fn", [
        openresponses_extended, openchat_extended,
        anthropic_extended, gemini_extended, ollama_extended,
        openrouter_extended,
    ])
    def test_returns_only_the_passthrough(self, fn) -> None:
        assert fn(_cfg(None)) == {}
        assert fn(_cfg(None, seed=7)) == {"seed": 7}


class TestOpenResponses:
    @pytest.mark.parametrize("level, effort", [
        (ReasoningLevel.OFF, "minimal"),   # no true off; minimal is the floor
        (ReasoningLevel.MINIMAL, "minimal"),
        (ReasoningLevel.LOW, "low"),
        (ReasoningLevel.MEDIUM, "medium"),
        (ReasoningLevel.HIGH, "high"),
    ])
    def test_maps_to_reasoning_effort(self, level, effort) -> None:
        assert openresponses_extended(_cfg(level)) == {"reasoning_effort": effort}

    def test_vllm_warns_and_emits_nothing(self, caplog) -> None:
        """VERIFIED against a live vLLM server: its Responses endpoint
        accepts every reasoning knob and honours none of them."""
        with caplog.at_level(logging.WARNING):
            out = openresponses_extended(_cfg(ReasoningLevel.OFF), flavor="vllm")
        assert out == {}, "must not emit a knob the server ignores"
        assert any("not supported on vLLM" in r.getMessage() for r in caplog.records)

    def test_vllm_still_forwards_the_passthrough(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            out = openresponses_extended(
                _cfg(ReasoningLevel.OFF, seed=3), flavor="vllm",
            )
        assert out == {"seed": 3}


class TestOpenChat:
    def test_vllm_uses_chat_template_kwargs(self) -> None:
        """VERIFIED working: enable_thinking=false yields reasoning: null
        and real content on the Chat Completions endpoint."""
        assert openchat_extended(_cfg(ReasoningLevel.OFF), flavor="vllm") == {
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def test_vllm_non_off_enables_thinking(self) -> None:
        assert openchat_extended(_cfg(ReasoningLevel.HIGH), flavor="vllm") == {
            "chat_template_kwargs": {"enable_thinking": True},
        }

    def test_real_openai_uses_reasoning_effort(self) -> None:
        assert openchat_extended(_cfg(ReasoningLevel.LOW), flavor="openai") == {
            "reasoning_effort": "low",
        }


class TestAnthropic:
    def test_off_disables_thinking(self) -> None:
        assert anthropic_extended(_cfg(ReasoningLevel.OFF)) == {
            "thinking": {"type": "disabled"},
        }

    def test_levels_carry_a_token_budget(self) -> None:
        out = anthropic_extended(_cfg(ReasoningLevel.HIGH))
        assert out["thinking"]["type"] == "enabled"
        assert out["thinking"]["budget_tokens"] > 0

    def test_budget_grows_with_level(self) -> None:
        budgets = [
            anthropic_extended(_cfg(lvl))["thinking"]["budget_tokens"]
            for lvl in (
                ReasoningLevel.MINIMAL, ReasoningLevel.LOW,
                ReasoningLevel.MEDIUM, ReasoningLevel.HIGH,
            )
        ]
        assert budgets == sorted(budgets)


class TestGeminiAndOllama:
    def test_gemini_off_is_a_zero_budget(self) -> None:
        assert gemini_extended(_cfg(ReasoningLevel.OFF)) == {"thinking_budget": 0}

    def test_gemini_budget_grows_with_level(self) -> None:
        assert (
            gemini_extended(_cfg(ReasoningLevel.HIGH))["thinking_budget"]
            > gemini_extended(_cfg(ReasoningLevel.LOW))["thinking_budget"]
        )

    def test_ollama_uses_a_boolean(self) -> None:
        assert ollama_extended(_cfg(ReasoningLevel.OFF)) == {"think": False}
        assert ollama_extended(_cfg(ReasoningLevel.MEDIUM)) == {"think": True}


class TestPrecedence:
    def test_explicit_level_wins_over_the_passthrough(self) -> None:
        """An operator who set both meant the typed field; the passthrough
        is the escape hatch for what the typed field cannot say."""
        cfg = _cfg(ReasoningLevel.HIGH, reasoning_effort="low")
        assert openresponses_extended(cfg)["reasoning_effort"] == "high"

    def test_unrelated_passthrough_keys_survive(self) -> None:
        cfg = _cfg(ReasoningLevel.HIGH, seed=11)
        out = openresponses_extended(cfg)
        assert out["seed"] == 11 and out["reasoning_effort"] == "high"


class TestOpenRouter:
    """OpenRouter normalises reasoning across its upstreams through one
    ``reasoning`` object; ``effort: "none"`` is its documented off switch."""

    @pytest.mark.parametrize("level, effort", [
        (ReasoningLevel.OFF, "none"),
        (ReasoningLevel.MINIMAL, "minimal"),
        (ReasoningLevel.LOW, "low"),
        (ReasoningLevel.MEDIUM, "medium"),
        (ReasoningLevel.HIGH, "high"),
    ])
    def test_maps_to_the_reasoning_object(self, level, effort) -> None:
        assert openrouter_extended(_cfg(level)) == {"reasoning": {"effort": effort}}

    def test_unrelated_passthrough_keys_survive(self) -> None:
        out = openrouter_extended(_cfg(ReasoningLevel.LOW, seed=5))
        assert out == {"seed": 5, "reasoning": {"effort": "low"}}


class _Translating:
    """Stands in for an adapter: records the configs it was asked to translate."""

    def __init__(self, out: dict) -> None:
        self.out = out
        self.seen: list[ModelProfileConfig] = []

    def profile_extended(self, config: ModelProfileConfig) -> dict:
        self.seen.append(config)
        return dict(self.out)


class _NoMapping:
    """An object with no ``profile_extended``: a duck-typed test double."""


class TestProfileExtended:
    """The one entry every call site uses to apply a profile's config."""

    def test_no_config_sends_nothing(self) -> None:
        llm = _Translating({"x": 1})
        assert profile_extended(llm, None) == {}
        assert profile_stream_kwargs(llm, None) == {}
        assert llm.seen == []

    def test_an_empty_config_sends_nothing_and_is_not_translated(self) -> None:
        """An empty config is inert: the call is made exactly as before."""
        llm = _Translating({"x": 1})
        assert profile_stream_kwargs(llm, ModelProfileConfig()) == {}
        assert llm.seen == []

    def test_something_that_is_not_a_profile_config_sends_nothing(self) -> None:
        """A test double's resolved model may carry anything (a MagicMock
        attribute); only a real ``ModelProfileConfig`` is applied."""
        llm = _Translating({"x": 1})
        assert profile_stream_kwargs(llm, object()) == {}  # type: ignore[arg-type]
        assert llm.seen == []

    def test_the_adapter_translates_for_its_own_wire(self) -> None:
        cfg = _cfg(ReasoningLevel.LOW, seed=3)
        llm = _Translating({"reasoning_effort": "low", "seed": 3})
        assert profile_stream_kwargs(llm, cfg) == {
            "extended": {"reasoning_effort": "low", "seed": 3},
        }
        assert llm.seen == [cfg]

    def test_a_translation_that_yields_nothing_sends_nothing(self) -> None:
        """vLLM's Responses endpoint, say: the level maps to no knob."""
        assert profile_stream_kwargs(_Translating({}), _cfg(ReasoningLevel.OFF)) == {}

    def test_an_object_without_a_mapping_gets_the_passthrough(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        out = profile_extended(_NoMapping(), _cfg(ReasoningLevel.HIGH, seed=9))
        assert out == {"seed": 9}
        assert any(
            "_NoMapping" in r.getMessage() and "high" in r.getMessage()
            for r in caplog.records if r.levelno == logging.DEBUG
        )

    def test_a_level_nothing_can_apply_is_logged_once_not_per_call(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        for _ in range(3):
            profile_extended(_NoMapping(), _cfg(ReasoningLevel.HIGH))
        hits = [r for r in caplog.records if "_NoMapping" in r.getMessage()]
        assert len(hits) == 1
