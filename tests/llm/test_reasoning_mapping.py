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
    claude_code_extended,
    gemini_extended,
    ollama_extended,
    openchat_extended,
    openresponses_extended,
    openrouter_extended,
    profile_extended,
    profile_stream,
)
from primer.llm.openresponses import _extract_extended_kwargs
from primer.llm.settings_catalogue import catalogue_for
from primer.model.chat import Done, Error, TextDelta
from primer.model.except_ import BadRequestError
from primer.model.model_profile import CustomReasoning, ModelProfileConfig, ReasoningLevel
from primer.model.providers.llm import OpenResponsesFlavor


@pytest.fixture(autouse=True)
def _fresh_once_log():
    """Each test sees the "log once" record and the refusal memo empty."""
    _reasoning._LOGGED_ONCE.clear()
    _reasoning._REFUSED.clear()
    yield
    _reasoning._LOGGED_ONCE.clear()
    _reasoning._REFUSED.clear()


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

    def test_vllm_emits_nothing_and_says_so_once_at_debug(self, caplog) -> None:
        """VERIFIED against a live vLLM server: its Responses endpoint
        accepts every reasoning knob and honours none of them. The notice is
        logged once per process at DEBUG; it used to be a WARNING on every
        call, which the review of #756 asked to stop."""
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        for _ in range(3):
            out = openresponses_extended(_cfg(ReasoningLevel.OFF), flavor="vllm")
            assert out == {}, "must not emit a knob the server ignores"
        notices = [r for r in caplog.records if "not supported on vLLM" in r.getMessage()]
        assert len(notices) == 1
        assert notices[0].levelno == logging.DEBUG
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

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

    def test_ollama_uses_reasoning_effort_with_none_for_off(self) -> None:
        """Audit G2-12: Ollama's /v1 reads reasoning_effort ("none" turns thinking off), not chat_template_kwargs."""
        assert openchat_extended(_cfg(ReasoningLevel.OFF), flavor="ollama") == {"reasoning_effort": "none"}
        assert openchat_extended(_cfg(ReasoningLevel.HIGH), flavor="ollama") == {"reasoning_effort": "high"}


class TestAnthropic:
    """The shape is the model's; the per-family table is pinned in
    tests/llm/test_profile_config_wiring.py."""

    def test_off_disables_thinking_on_a_budget_model(self) -> None:
        assert anthropic_extended(_cfg(ReasoningLevel.OFF), model="claude-haiku-4-5") == {
            "thinking": {"type": "disabled"},
        }

    def test_levels_carry_a_token_budget_on_a_budget_model(self) -> None:
        out = anthropic_extended(_cfg(ReasoningLevel.HIGH), model="claude-haiku-4-5")
        assert out["thinking"]["type"] == "enabled"
        assert out["thinking"]["budget_tokens"] > 0

    def test_budget_grows_with_level(self) -> None:
        budgets = [
            anthropic_extended(_cfg(lvl), model="claude-sonnet-4-5")["thinking"]["budget_tokens"]
            for lvl in (
                ReasoningLevel.MINIMAL, ReasoningLevel.LOW,
                ReasoningLevel.MEDIUM, ReasoningLevel.HIGH,
            )
        ]
        assert budgets == sorted(budgets)

    def test_effort_grows_with_level_on_an_adaptive_model(self) -> None:
        efforts = [
            anthropic_extended(_cfg(lvl), model="claude-opus-4-7")["output_config"]["effort"]
            for lvl in (
                ReasoningLevel.MINIMAL, ReasoningLevel.LOW,
                ReasoningLevel.MEDIUM, ReasoningLevel.HIGH,
            )
        ]
        assert efforts == ["low", "low", "medium", "high"]


class TestGeminiAndOllama:
    """The Gemini shape is the model's; the per-family table is pinned in
    tests/llm/test_profile_config_wiring.py."""

    def test_gemini_2_5_flash_off_is_a_zero_budget(self) -> None:
        assert gemini_extended(_cfg(ReasoningLevel.OFF), model="gemini-2.5-flash") == {
            "thinking_budget": 0,
        }

    def test_gemini_budget_grows_with_level(self) -> None:
        assert (
            gemini_extended(_cfg(ReasoningLevel.HIGH), model="gemini-2.5-flash")["thinking_budget"]
            > gemini_extended(_cfg(ReasoningLevel.LOW), model="gemini-2.5-flash")["thinking_budget"]
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


# One id per row of the tables the mapping indexes, plus the no-control and
# unknown rows (spec section 4, plan review focus 2).
_EFFORT_MODELS = [
    "o3", "gpt-5-codex", "gpt-5", "gpt-5.1", "gpt-6", "gpt-5-pro",
    "gpt-4o", "o1-mini", "my-deployment",
]
_CLAUDE_MODELS = [
    "claude-opus-4-0", "claude-sonnet-4-5", "claude-haiku-4-5",  # budget
    "claude-opus-4-6", "claude-sonnet-4-6",                      # adaptive, 4.6
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",       # adaptive, 4.7 and later
    "claude-sonnet-5", "claude-haiku-5-5", "claude-sonnet-5-5",  # adaptive, the rest
    "claude-opus-5-5", "claude-fable-5-1",                       # adaptive, cannot turn off
    "claude-3-5-haiku-20241022", "my-proxy-model",               # no control / unknown
]
_GEMINI_MODELS = [
    "gemini-4-argon", "gemini-3.8-flash", "gemini-3.1-pro",
    "gemini-3.1-flash-lite-image", "gemini-3-pro",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", None,
]


def _custom(value: str) -> ModelProfileConfig:
    return ModelProfileConfig(reasoning=CustomReasoning(custom=value))


class TestExhaustiveMapping:
    """Every preset, including xhigh and max, reaches every wire and every table row: no KeyError anywhere."""

    @pytest.mark.parametrize("level", list(ReasoningLevel))
    @pytest.mark.parametrize("model", _EFFORT_MODELS)
    @pytest.mark.parametrize("fn, flavor", [
        (openresponses_extended, "openai"), (openresponses_extended, "lmstudio"),
        (openresponses_extended, "other"), (openresponses_extended, "strata"),
        (openchat_extended, "openai"), (openchat_extended, "ollama"),
        (openchat_extended, "vllm"), (openchat_extended, "lmstudio"),
        (openchat_extended, "other"), (openchat_extended, "strata"),
    ])
    def test_the_openai_wires(self, level, model, fn, flavor) -> None:
        assert isinstance(fn(_cfg(level), flavor=flavor, model=model), dict)

    @pytest.mark.parametrize("level", list(ReasoningLevel))
    @pytest.mark.parametrize("model", _CLAUDE_MODELS)
    def test_the_anthropic_wire(self, level, model) -> None:
        assert isinstance(anthropic_extended(_cfg(level), model=model), dict)

    @pytest.mark.parametrize("level", list(ReasoningLevel))
    @pytest.mark.parametrize("model", _GEMINI_MODELS)
    def test_the_gemini_wire(self, level, model) -> None:
        assert isinstance(gemini_extended(_cfg(level), model=model), dict)

    @pytest.mark.parametrize("level", list(ReasoningLevel))
    @pytest.mark.parametrize("fn", [ollama_extended, openrouter_extended, claude_code_extended])
    def test_the_modelless_wires(self, level, fn) -> None:
        assert isinstance(fn(_cfg(level)), dict)


class TestXHighAndMax:
    """The two new presets: clamped to what the tables have verified (spec section 4)."""

    @pytest.mark.parametrize("model", ["o3", "gpt-5-codex", "gpt-5", "gpt-5.1", "gpt-6", "gpt-5-pro", "my-deployment"])
    def test_openai_families_send_what_high_sends(self, model) -> None:
        for wire in (openresponses_extended, openchat_extended):
            for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
                assert wire(_cfg(level), model=model)["reasoning_effort"] == "high"

    @pytest.mark.parametrize("level, effort", [
        (ReasoningLevel.XHIGH, "high"),  # max but not xhigh is recorded
        (ReasoningLevel.MAX, "max"),
    ])
    def test_anthropic_opus_4_6(self, level, effort) -> None:
        out = anthropic_extended(_cfg(level), model="claude-opus-4-6")
        assert out["output_config"]["effort"] == effort
        assert out["thinking"] == {"type": "adaptive"}

    @pytest.mark.parametrize("model", ["claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5"])
    def test_anthropic_opus_4_7_and_later_send_xhigh_and_max(self, model) -> None:
        assert anthropic_extended(_cfg(ReasoningLevel.XHIGH), model=model)["output_config"]["effort"] == "xhigh"
        assert anthropic_extended(_cfg(ReasoningLevel.MAX), model=model)["output_config"]["effort"] == "max"

    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-sonnet-5", "claude-haiku-5-5", "claude-sonnet-5-5"])
    def test_anthropic_the_other_adaptive_models_send_high_for_both(self, model) -> None:
        for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
            out = anthropic_extended(_cfg(level), model=model)
            assert out["output_config"]["effort"] == "high"
            assert out["thinking"] == {"type": "adaptive"}

    @pytest.mark.parametrize("model", ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"])
    def test_anthropic_budget_models_send_the_high_budget_for_both(self, model) -> None:
        for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
            assert anthropic_extended(_cfg(level), model=model) == {
                "thinking": {"type": "enabled", "budget_tokens": 32768},
            }

    def test_openrouter_sends_xhigh_and_max(self) -> None:
        assert openrouter_extended(_cfg(ReasoningLevel.XHIGH)) == {"reasoning": {"effort": "xhigh"}}
        assert openrouter_extended(_cfg(ReasoningLevel.MAX)) == {"reasoning": {"effort": "max"}}

    def test_gemini_sends_the_models_top_level_or_budget(self) -> None:
        for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
            # a 3+ model: its top level; a 2.5 model: its top budget
            assert gemini_extended(_cfg(level), model="gemini-3-pro")["thinking_level"] == "high"
            assert gemini_extended(_cfg(level), model="gemini-2.5-flash")["thinking_budget"] == 24576
            assert gemini_extended(_cfg(level), model="gemini-2.5-pro")["thinking_budget"] == 32768

    def test_ollama_thinks_for_both(self) -> None:
        assert ollama_extended(_cfg(ReasoningLevel.XHIGH)) == {"think": True}
        assert ollama_extended(_cfg(ReasoningLevel.MAX)) == {"think": True}

    def test_openchat_ollama_sends_high_for_both(self) -> None:
        for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
            assert openchat_extended(_cfg(level), flavor="ollama") == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("flavor", ["vllm", "lmstudio", "other"])
    def test_openchat_template_flavors_enable_thinking_for_both(self, flavor) -> None:
        for level in (ReasoningLevel.XHIGH, ReasoningLevel.MAX):
            assert openchat_extended(_cfg(level), flavor=flavor) == {
                "chat_template_kwargs": {"enable_thinking": True},
            }

    def test_claude_code_sends_xhigh_and_max(self) -> None:
        assert claude_code_extended(_cfg(ReasoningLevel.XHIGH)) == {"effort": "xhigh"}
        assert claude_code_extended(_cfg(ReasoningLevel.MAX)) == {"effort": "max"}


class TestCustom:
    """A hand-written value: sent verbatim to the wire's custom target, no mapping, no clamping."""

    def test_an_openai_family_gets_the_value_verbatim(self) -> None:
        """A family a preset would clamp for still gets the value as written."""
        assert openresponses_extended(_custom("xhigh"), flavor="openai", model="o3") == {"reasoning_effort": "xhigh"}
        assert openchat_extended(_custom("ultra"), flavor="openai", model="gpt-5.1") == {
            "reasoning_effort": "ultra",
        }

    def test_openchat_vllm_gets_reasoning_effort_and_no_template_kwargs(self) -> None:
        assert openchat_extended(_custom("high"), flavor="vllm") == {"reasoning_effort": "high"}

    def test_openchat_ollama_gets_reasoning_effort(self) -> None:
        assert openchat_extended(_custom("deep"), flavor="ollama") == {"reasoning_effort": "deep"}

    def test_anthropic_adaptive_gets_the_effort_and_adaptive_thinking(self) -> None:
        out = anthropic_extended(_custom("max"), model="claude-opus-5-5")
        assert out["output_config"]["effort"] == "max"
        assert out["thinking"] == {"type": "adaptive"}

    def test_anthropic_budget_gets_only_the_effort(self) -> None:
        assert anthropic_extended(_custom("max"), model="claude-haiku-4-5") == {
            "output_config": {"effort": "max"},
        }

    def test_gemini_gets_the_level_verbatim(self) -> None:
        assert gemini_extended(_custom("LOW"), model="gemini-3-pro") == {"thinking_level": "LOW"}

    def test_openrouter_gets_the_effort(self) -> None:
        assert openrouter_extended(_custom("xhigh")) == {"reasoning": {"effort": "xhigh"}}

    def test_ollama_gets_the_value(self) -> None:
        assert ollama_extended(_custom("high")) == {"think": "high"}

    def test_claude_code_gets_the_effort(self) -> None:
        assert claude_code_extended(_custom("xhigh")) == {"effort": "xhigh"}


class TestStrata:
    """Strata, a flavor of both OpenAI wires: its own levels, none a true off
    (spec section 8). xhigh and max are not verified there: both send what
    high sends."""

    @pytest.mark.parametrize("level, expected", [
        (ReasoningLevel.OFF, "none"),
        (ReasoningLevel.MINIMAL, "low"),
        (ReasoningLevel.LOW, "low"),
        (ReasoningLevel.MEDIUM, "medium"),
        (ReasoningLevel.HIGH, "high"),
        (ReasoningLevel.XHIGH, "high"),
        (ReasoningLevel.MAX, "high"),
    ])
    def test_presets_send_the_models_own_level(self, level, expected) -> None:
        assert openchat_extended(_cfg(level), flavor="strata") == {
            "reasoning_effort": expected,
        }
        assert openresponses_extended(_cfg(level), flavor="strata") == {
            "reasoning_effort": expected,
        }

    @pytest.mark.parametrize("level, expected", [
        (ReasoningLevel.OFF, "none"),
        (ReasoningLevel.MINIMAL, "low"),
        (ReasoningLevel.LOW, "low"),
        (ReasoningLevel.MEDIUM, "medium"),
        (ReasoningLevel.HIGH, "high"),
        (ReasoningLevel.XHIGH, "high"),
        (ReasoningLevel.MAX, "high"),
    ])
    def test_presets_survive_the_responses_extractor(self, caplog, level, expected) -> None:
        """The mapping and the adapter's extractor agree (the layer boundary the nested shape broke): for
        every preset the mapped dict, folded by the strata wire, is ``reasoning.effort`` with nothing
        dropped and no warning. A unique provider id per preset keeps the once-per-provider dedupe from
        hiding a later warning."""
        with caplog.at_level(logging.WARNING, logger="primer.llm.settings_catalogue"):
            out = _extract_extended_kwargs(
                openresponses_extended(_cfg(level), flavor="strata"),
                catalogue=catalogue_for("openresponses/strata"),
                flavor=OpenResponsesFlavor.STRATA,
                provider_id=f"t6-strata-{level.value}",
            )
        assert out == {"reasoning": {"effort": expected}}
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], [r.message for r in caplog.records]

    def test_a_custom_value_goes_to_the_same_field_verbatim(self) -> None:
        assert openchat_extended(_custom("xhigh"), flavor="strata") == {
            "reasoning_effort": "xhigh",
        }
        assert openresponses_extended(_custom("max"), flavor="strata") == {
            "reasoning_effort": "max",
        }


class TestDeepMerge:
    """The level wins only on the leaf it sets; sibling keys in the same object stay."""

    def test_deep_merge_keeps_siblings(self) -> None:
        cfg = _cfg(ReasoningLevel.HIGH, chat_template_kwargs={"enable_thinking": False, "foo": 1})
        assert openchat_extended(cfg, flavor="vllm") == {
            "chat_template_kwargs": {"enable_thinking": True, "foo": 1},
        }
        cfg = _cfg(ReasoningLevel.LOW, thinking={"display": "omitted"})
        out = anthropic_extended(cfg, model="claude-opus-4-7")
        assert out["thinking"] == {"type": "adaptive", "display": "omitted"}
        assert out["output_config"] == {"effort": "low"}

    def test_level_still_wins_on_its_leaf(self) -> None:
        cfg = _cfg(ReasoningLevel.LOW, reasoning_effort="high")
        assert openresponses_extended(cfg)["reasoning_effort"] == "low"


class _Translating:
    """Stands in for an adapter: records the configs (and models) it was asked to translate."""

    def __init__(self, out: dict) -> None:
        self.out = out
        self.seen: list[tuple[ModelProfileConfig, str | None]] = []

    def profile_extended(self, config: ModelProfileConfig, *, model: str | None = None) -> dict:
        self.seen.append((config, model))
        return dict(self.out)


class _NoMapping:
    """An object with no ``profile_extended``: a duck-typed test double."""


class TestProfileExtended:
    """The one entry every call site uses to translate a profile's config."""

    def test_no_config_sends_nothing(self) -> None:
        llm = _Translating({"x": 1})
        assert profile_extended(llm, None) == {}
        assert llm.seen == []

    def test_an_empty_config_sends_nothing_and_is_not_translated(self) -> None:
        """An empty config is inert: the call is made exactly as before."""
        llm = _Translating({"x": 1})
        assert profile_extended(llm, ModelProfileConfig(), model="m") == {}
        assert llm.seen == []

    def test_something_that_is_not_a_profile_config_sends_nothing(self) -> None:
        """A test double's resolved model may carry anything (a MagicMock
        attribute); only a real ``ModelProfileConfig`` is applied."""
        llm = _Translating({"x": 1})
        assert profile_extended(llm, object(), model="m") == {}  # type: ignore[arg-type]
        assert llm.seen == []

    def test_the_adapter_translates_for_its_wire_and_the_model(self) -> None:
        cfg = _cfg(ReasoningLevel.LOW, seed=3)
        llm = _Translating({"reasoning_effort": "low", "seed": 3})
        assert profile_extended(llm, cfg, model="gpt-5") == {
            "reasoning_effort": "low", "seed": 3,
        }
        assert llm.seen == [(cfg, "gpt-5")]

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


_OK = [TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]


class _Provider:
    """A fake adapter whose answer depends on what the call carried.

    ``refuse`` decides, from a call's ``extended``, how the provider refuses
    it: ``"raise"`` (an eager adapter's BadRequestError at stream open),
    ``"yield"`` (a lazily-opened adapter's fatal first event), or ``None``.
    """

    def __init__(self, refuse, *, message: str = "Unsupported parameter: 'reasoning_effort'",
                 code: str | None = "bad_request", after_first: bool = False) -> None:
        self.refuse = refuse
        self.message = message
        self.code = code
        self.after_first = after_first
        self.calls: list[dict] = []
        self.closed = 0

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        how = self.refuse(kwargs.get("extended"))

        async def _gen():
            try:
                if how == "raise":
                    raise BadRequestError(self.message, code=self.code, status_code=400)
                if self.after_first and how == "yield":
                    yield TextDelta(text="partial", index=0)
                if how == "yield":
                    yield Error(fatal=True, code=self.code, message=self.message)
                    return
                for ev in _OK:
                    yield ev
            finally:
                self.closed += 1

        return _gen()


def _refuses_knobs(extended):
    return "raise" if extended and "reasoning_effort" in extended else None


async def _drain(it) -> list:
    return [ev async for ev in it]


class TestProfileStream:
    """A profile setting must never turn a call that works into one that fails."""

    async def test_no_knobs_is_the_plain_call(self) -> None:
        llm = _Provider(lambda ext: None)
        await _drain(profile_stream(llm, {}, model="m", messages=[]))
        assert llm.calls == [{"model": "m", "messages": []}]

    async def test_the_knobs_ride_under_the_callers_extended(self) -> None:
        llm = _Provider(lambda ext: None)
        await _drain(profile_stream(
            llm, {"reasoning_effort": "low", "seed": 1},
            model="m", messages=[], extended={"seed": 2},
        ))
        assert llm.calls[0]["extended"] == {"reasoning_effort": "low", "seed": 2}

    async def test_a_refusal_at_open_is_retried_once_without_the_settings(self, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="primer.llm._reasoning")
        llm = _Provider(_refuses_knobs)
        events = await _drain(profile_stream(
            llm, {"reasoning_effort": "minimal"}, model="gpt-4o", messages=[],
        ))
        assert [type(e).__name__ for e in events] == ["TextDelta", "Done"]
        assert len(llm.calls) == 2
        assert "extended" not in llm.calls[1]
        assert any("gpt-4o" in r.getMessage() and "reasoning_effort" in r.getMessage()
                   for r in caplog.records)

    async def test_a_yielded_refusal_at_open_is_retried_too(self) -> None:
        """Ollama and Gemini open lazily and report a 400 as a fatal first event."""
        llm = _Provider(
            lambda ext: "yield" if ext else None,
            message='"llama3.2" does not support thinking',
        )
        events = await _drain(profile_stream(llm, {"think": True}, model="llama3.2", messages=[]))
        assert [type(e).__name__ for e in events] == ["TextDelta", "Done"]
        assert len(llm.calls) == 2

    async def test_the_caller_extended_stays_on_the_retry(self) -> None:
        llm = _Provider(_refuses_knobs)
        await _drain(profile_stream(
            llm, {"reasoning_effort": "low"}, model="m", messages=[], extended={"seed": 2},
        ))
        assert llm.calls[1]["extended"] == {"seed": 2}

    async def test_a_refused_setting_costs_one_failed_request_not_one_per_call(self) -> None:
        llm = _Provider(_refuses_knobs)
        for _ in range(3):
            await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert len(llm.calls) == 4  # one refusal, then three plain calls
        assert all("extended" not in c for c in llm.calls[1:])

    async def test_the_memo_belongs_to_the_adapter_instance_not_its_id(self) -> None:
        """An id() is reused once its object is collected; a new adapter (a
        provider edit builds one) must try the setting itself."""
        import gc

        a = _Provider(_refuses_knobs)
        await _drain(profile_stream(a, {"reasoning_effort": "low"}, model="m", messages=[]))
        b = _Provider(_refuses_knobs)
        await _drain(profile_stream(b, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert b.calls[0]["extended"] == {"reasoning_effort": "low"}
        assert len(_reasoning._REFUSED) == 2
        del a, b
        gc.collect()
        assert len(_reasoning._REFUSED) == 0
        c = _Provider(_refuses_knobs)  # may well reuse a collected adapter's id
        await _drain(profile_stream(c, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert c.calls[0]["extended"] == {"reasoning_effort": "low"}

    async def test_an_adapter_that_cannot_be_weakly_referenced_still_falls_back(self) -> None:
        import weakref

        class _Slotted:
            __slots__ = ("calls",)

            def __init__(self) -> None:
                self.calls: list[dict] = []

            def stream(self, **kwargs):
                self.calls.append(kwargs)
                refuse = "extended" in kwargs

                async def _gen():
                    if refuse:
                        raise BadRequestError("Unsupported parameter: 'reasoning_effort'", status_code=400)
                    for ev in _OK:
                        yield ev

                return _gen()

        llm = _Slotted()
        with pytest.raises(TypeError):
            weakref.ref(llm)
        for _ in range(2):
            events = await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
            assert [type(e).__name__ for e in events] == ["TextDelta", "Done"]
        # Nothing can be remembered for it, so each call tries the setting once.
        assert [("extended" in c) for c in llm.calls] == [True, False, True, False]

    async def test_another_model_or_setting_is_tried_afresh(self) -> None:
        llm = _Provider(_refuses_knobs)
        await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="other", messages=[]))
        assert llm.calls[2]["extended"] == {"reasoning_effort": "low"}

    async def test_a_400_that_names_no_setting_is_not_retried(self) -> None:
        llm = _Provider(_refuses_knobs, message="prompt is too long: 250000 tokens > 200000 maximum")
        with pytest.raises(BadRequestError):
            await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert len(llm.calls) == 1

    async def test_a_transient_failure_is_not_a_refusal(self) -> None:
        llm = _Provider(lambda ext: "yield", message="reasoning server overloaded", code="rate_limit")
        events = await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert len(llm.calls) == 1
        assert isinstance(events[0], Error) and events[0].code == "rate_limit"

    async def test_a_failure_after_the_first_event_is_never_retried(self) -> None:
        """Output already reached the consumer; a retry would repeat it."""
        llm = _Provider(lambda ext: "yield", after_first=True)
        events = await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert len(llm.calls) == 1
        assert [type(e).__name__ for e in events] == ["TextDelta", "Error"]

    async def test_a_retry_that_fails_too_is_not_remembered(self) -> None:
        """The setting was not the cause; the next call carries it again."""
        llm = _Provider(lambda ext: "raise")
        for _ in range(2):
            with pytest.raises(BadRequestError):
                await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[]))
        assert [("extended" in c) for c in llm.calls] == [True, False, True, False]

    async def test_a_consumer_that_stops_early_closes_the_provider_stream(self) -> None:
        llm = _Provider(lambda ext: None)
        stream = profile_stream(llm, {"reasoning_effort": "low"}, model="m", messages=[])
        await stream.__anext__()
        await stream.aclose()
        assert llm.closed == 1

    async def test_openai_moderation_text_is_not_a_refusal_of_reasoning_effort(self) -> None:
        """It links to OpenAI's reasoning guide; it names no setting the call sent."""
        llm = _Provider(lambda ext: "raise", message=(
            "Invalid prompt: your prompt was flagged as potentially violating our "
            "usage policy. Please try again with a different prompt: "
            "https://platform.openai.com/docs/guides/reasoning#advice-on-prompting"
        ))
        with pytest.raises(BadRequestError):
            await _drain(profile_stream(llm, {"reasoning_effort": "low"}, model="o3", messages=[]))
        assert len(llm.calls) == 1

    async def test_a_seed_only_profile_is_not_retried_on_a_thinking_400(self) -> None:
        llm = _Provider(lambda ext: "raise", message="thinking is not supported for this model")
        with pytest.raises(BadRequestError):
            await _drain(profile_stream(llm, {"seed": 1}, model="m", messages=[]))
        assert len(llm.calls) == 1

    @pytest.mark.parametrize("knobs, message", [
        ({"reasoning_effort": "low"}, "Unsupported parameter: 'reasoning.effort' is not supported with this model."),
        ({"thinking": {"type": "enabled", "budget_tokens": 1024}}, '"thinking.type.enabled" is not supported for this model'),
        ({"thinking": {"type": "enabled", "budget_tokens": 1024}}, "budget_tokens: must be less than max_tokens"),
        ({"output_config": {"effort": "low"}}, "output_config.effort: Extra inputs are not permitted"),
        ({"thinking_budget": 0}, "Budget 0 is invalid. This model only works in thinking mode."),
        ({"thinking_level": "low"}, "Thinking level is not supported for this model."),
        ({"thinking_budget": 1024}, "Thinking is not supported for this model."),
        ({"thinking_level": "low"}, "Thinking is not supported for this model."),
        ({"think": True}, '"llama3.2" does not support thinking'),
        ({"chat_template_kwargs": {"enable_thinking": False}}, "Extra inputs are not permitted: body.chat_template_kwargs"),
        ({"reasoning": {"effort": "none"}}, "Reasoning is mandatory for this endpoint and cannot be disabled."),
        ({"seed": 1}, "seed is not supported by this model"),
    ])
    async def test_each_setting_is_recognised_by_its_own_words(self, knobs, message) -> None:
        llm = _Provider(lambda ext: "raise" if ext else None, message=message)
        await _drain(profile_stream(llm, knobs, model="m", messages=[]))
        assert len(llm.calls) == 2

    async def test_a_key_is_matched_as_a_word_not_inside_another(self) -> None:
        """A passthrough ``user`` knob must not match "username" in an unrelated 400."""
        llm = _Provider(lambda ext: "raise", message="invalid username field")
        with pytest.raises(BadRequestError):
            await _drain(profile_stream(llm, {"user": "u-1"}, model="m", messages=[]))
        assert len(llm.calls) == 1
