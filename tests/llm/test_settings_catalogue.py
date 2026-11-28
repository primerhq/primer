"""The per-wire model settings catalogue and its validator (PMS Task 1).

Spec: docs/superpowers/specs/2026-11-26-profile-model-settings-design.md,
sections 3 and 5 (the table is the catalogue's content).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from primer.llm.settings_catalogue import (
    catalogue_for,
    validate_extended,
    warn_dropped,
    wire_of,
)
from primer.model.providers.llm import LLMProviderType, OpenChatFlavor, OpenResponsesFlavor

MODULE_PATH = Path(__file__).resolve().parents[2] / "primer" / "llm" / "settings_catalogue.py"

SHARED_CHAT_COMPLETIONS = frozenset(
    {
        "parallel_tool_calls",
        "presence_penalty",
        "frequency_penalty",
        "logprobs",
        "top_logprobs",
        "seed",
        "user",
    }
)

OPENRESPONSES_SET = frozenset(
    {
        "reasoning_effort",
        "reasoning_summary",
        "reasoning_context",
        "reasoning_mode",
        "verbosity",
        "service_tier",
        "prompt_cache_key",
        "prompt_cache_retention",
        "metadata",
        "max_tool_calls",
        "top_logprobs",
        "parallel_tool_calls",
        "truncation",
        "safety_identifier",
    }
)

WIRE_KEYS: dict[str, frozenset[str]] = {
    "openchat/openai": frozenset(
        {
            "reasoning_effort",
            "verbosity",
            "service_tier",
            "prompt_cache_key",
            "prompt_cache_retention",
            "safety_identifier",
        }
        | SHARED_CHAT_COMPLETIONS
    ),
    "openchat/vllm": frozenset(
        {
            "reasoning_effort",
            "chat_template_kwargs",
            "thinking_token_budget",
            "include_reasoning",
            "top_k",
            "min_p",
            "repetition_penalty",
            "min_tokens",
            "priority",
        }
        | SHARED_CHAT_COMPLETIONS
    ),
    "openchat/lmstudio": frozenset(
        {
            "reasoning_effort",
            "chat_template_kwargs",
            "reasoning_tokens",
            "ttl",
            "draft_model",
            "top_k",
            "min_p",
            "repeat_penalty",
        }
        | SHARED_CHAT_COMPLETIONS
    ),
    "openchat/ollama": frozenset({"reasoning_effort"} | SHARED_CHAT_COMPLETIONS),
    "openchat/other": frozenset({"reasoning_effort", "chat_template_kwargs"} | SHARED_CHAT_COMPLETIONS),
    "openchat/strata": frozenset(
        {
            "reasoning_effort",
            "reasoning_budget_tokens",
            "top_k",
            "min_p",
            "repetition_penalty",
            "penalty_last_n",
            "chat_template_kwargs",
            "return_progress",
        }
        | SHARED_CHAT_COMPLETIONS
    ),
    "openresponses/openai": OPENRESPONSES_SET,
    "openresponses/lmstudio": OPENRESPONSES_SET,
    "openresponses/vllm": OPENRESPONSES_SET,
    "openresponses/other": OPENRESPONSES_SET,
    "openresponses/strata": frozenset(
        {
            "reasoning_effort",
            "reasoning_budget_tokens",
            "top_k",
            "min_p",
            "repetition_penalty",
            "penalty_last_n",
            "parallel_tool_calls",
            "max_tool_calls",
            "top_logprobs",
        }
    ),
    "anthropic": frozenset(
        {
            "thinking",
            "output_config",
            "top_k",
            "service_tier",
            "metadata",
            "cache_control",
            "speed",
            "inference_geo",
        }
    ),
    "gemini": frozenset(
        {
            "thinking_level",
            "thinking_budget",
            "include_thoughts",
            "top_k",
            "seed",
            "presence_penalty",
            "frequency_penalty",
            "safety_settings",
            "logprobs",
            "response_logprobs",
            "media_resolution",
            "service_tier",
        }
    ),
    "ollama": frozenset(
        {
            "think",
            "keep_alive",
            "top_k",
            "seed",
            "repeat_penalty",
            "frequency_penalty",
            "presence_penalty",
            "mirostat",
            "mirostat_tau",
            "mirostat_eta",
            "tfs_z",
            "typical_p",
            "num_ctx",
            "num_batch",
            "num_gpu",
            "min_p",
            "repeat_last_n",
        }
    ),
    "openrouter": frozenset(
        {"reasoning", "verbosity", "provider", "top_k", "min_p", "top_a", "repetition_penalty"}
        | SHARED_CHAT_COMPLETIONS
    ),
    "codex": frozenset({"reasoning_effort"}),
    "claude_code": frozenset({"effort"}),
}

CUSTOM_KEYS: dict[str, str] = {
    "openchat/openai": "reasoning_effort",
    "openchat/vllm": "reasoning_effort",
    "openchat/lmstudio": "reasoning_effort",
    "openchat/ollama": "reasoning_effort",
    "openchat/other": "reasoning_effort",
    "openchat/strata": "reasoning_effort",
    "openresponses/openai": "reasoning_effort",
    "openresponses/lmstudio": "reasoning_effort",
    "openresponses/vllm": "reasoning_effort",
    "openresponses/other": "reasoning_effort",
    "openresponses/strata": "reasoning_effort",
    "anthropic": "output_config.effort",
    "gemini": "thinking_level",
    "ollama": "think",
    "openrouter": "reasoning.effort",
    "codex": "reasoning_effort",
    "claude_code": "effort",
}


def test_wire_of():
    assert wire_of("openchat", "vllm") == "openchat/vllm"
    assert wire_of("anthropic", None) == "anthropic"
    assert wire_of("openresponses", "openai") == "openresponses/openai"


def test_every_wire_has_a_catalogue():
    # Task 6 adds the strata wires; until then the enums carry no strata.
    for provider in LLMProviderType:
        if provider is LLMProviderType.OPENCHAT:
            flavours = [f.value for f in OpenChatFlavor]
        elif provider is LLMProviderType.OPENRESPONSES:
            flavours = [f.value for f in OpenResponsesFlavor]
        else:
            flavours = [None]
        for flavour in flavours:
            catalogue = catalogue_for(wire_of(provider.value, flavour))
            assert catalogue.settings, f"{provider.value}/{flavour} has no settings"
            assert catalogue.wire == wire_of(provider.value, flavour)


def test_catalogue_keys():
    for wire, expected in WIRE_KEYS.items():
        assert catalogue_for(wire).keys() == expected, wire


def test_custom_target_is_an_open_enum():
    for wire, key in CUSTOM_KEYS.items():
        catalogue = catalogue_for(wire)
        assert catalogue.custom_reasoning_key == key, wire
        setting = catalogue.custom_reasoning_setting()
        assert setting.type == "enum", wire
        assert setting.open, wire
        assert setting.key == key.rsplit(".", 1)[-1], wire


def test_open_enum_takes_any_string():
    assert validate_extended("openchat/openai", {"reasoning_effort": "ultra"}) == []
    problems = validate_extended("openchat/openai", {"reasoning_effort": ""})
    assert len(problems) == 1
    assert problems[0].path == "reasoning_effort"


def test_unknown_key_message():
    problems = validate_extended("openchat/openai", {"topk": 5})
    assert len(problems) == 1
    assert problems[0].path == "topk"
    accepted = ", ".join(sorted(WIRE_KEYS["openchat/openai"]))
    assert problems[0].message == (
        f"extended.topk is not a setting for openchat/openai; accepted: {accepted}"
    )


def test_bool_is_not_a_number():
    assert len(validate_extended("openchat/vllm", {"top_k": True})) == 1
    assert validate_extended("openchat/vllm", {"top_k": 20}) == []


def test_closed_enum_values_are_typed():
    assert validate_extended("openrouter", {"verbosity": "loud"})
    assert validate_extended("anthropic", {"speed": "fast"}) == []
    assert validate_extended("anthropic", {"speed": "turbo"})


def test_range():
    assert validate_extended("openchat/vllm", {"min_p": 1.5})
    assert validate_extended("openchat/vllm", {"min_p": 0.05}) == []


class TestStrataCatalogue:
    """The strata wires (spec section 8): both OpenAI wires, one server."""

    @pytest.mark.parametrize("wire", ["openchat/strata", "openresponses/strata"])
    def test_the_wires_allow_body_passthrough(self, wire) -> None:
        assert catalogue_for(wire).body_passthrough is True, wire

    @pytest.mark.parametrize("wire", ["openchat/strata", "openresponses/strata"])
    def test_top_k_is_bounded_one_to_sixty_four(self, wire) -> None:
        assert validate_extended(wire, {"top_k": 65}), wire
        assert validate_extended(wire, {"top_k": 0}), wire
        assert validate_extended(wire, {"top_k": 64}) == [], wire

    @pytest.mark.parametrize("wire", ["openchat/strata", "openresponses/strata"])
    def test_penalty_last_n_has_a_floor(self, wire) -> None:
        assert validate_extended(wire, {"penalty_last_n": -1}), wire
        assert validate_extended(wire, {"penalty_last_n": 0}) == [], wire

    @pytest.mark.parametrize("wire", ["openchat/strata", "openresponses/strata"])
    def test_min_p_is_bounded_zero_to_one(self, wire) -> None:
        assert validate_extended(wire, {"min_p": 1.5}), wire
        assert validate_extended(wire, {"min_p": 0.5}) == [], wire

    @pytest.mark.parametrize("wire", ["openchat/strata", "openresponses/strata"])
    def test_reasoning_effort_is_an_open_enum(self, wire) -> None:
        """The levels the model was trained with are suggestions; any value passes."""
        assert validate_extended(wire, {"reasoning_effort": "ultra"}) == [], wire


def test_closed_object_inner_key():
    assert (
        validate_extended("anthropic", {"thinking": {"type": "adaptive", "display": "omitted"}}) == []
    )
    problems = validate_extended("anthropic", {"thinking": {"type": "adaptive", "budget": 5}})
    assert len(problems) == 1
    assert problems[0].path == "thinking.budget"
    problems = validate_extended("anthropic", {"thinking": {"type": "enabled", "budget_tokens": 512}})
    assert len(problems) == 1
    assert problems[0].path == "thinking.budget_tokens"


def test_open_object_takes_any_inner_key():
    assert (
        validate_extended("openchat/vllm", {"chat_template_kwargs": {"enable_thinking": False, "foo": 1}})
        == []
    )


def test_body():
    assert validate_extended("openchat/vllm", {"body": {"return_progress": True}}) == []
    problems = validate_extended("openchat/vllm", {"body": {"model": "x"}})
    assert [p.path for p in problems] == ["body.model"]
    problems = validate_extended("openresponses/vllm", {"body": {"previous_response_id": "r"}})
    assert [p.path for p in problems] == ["body.previous_response_id"]
    problems = validate_extended("openchat/vllm", {"body": {"top_k": 5}, "top_k": 5})
    assert any(p.path == "body.top_k" for p in problems)
    # body on a wire without body passthrough is an unknown key
    problems = validate_extended("openchat/openai", {"body": {}})
    assert [p.path for p in problems] == ["body"]
    assert "is not a setting for openchat/openai" in problems[0].message


def test_openrouter_effort_or_max_tokens():
    problems = validate_extended("openrouter", {"reasoning": {"effort": "high", "max_tokens": 100}})
    assert problems


def test_openrouter_reasoning_exclude_is_boolean():
    assert validate_extended("openrouter", {"reasoning": {"exclude": True}}) == []
    problems = validate_extended("openrouter", {"reasoning": {"exclude": "yes"}})
    assert len(problems) == 1
    assert problems[0].path == "reasoning.exclude"


def test_no_vendor_sdk_import():
    # primer.llm.__init__ imports the adapters (and their SDKs), so a package
    # import can never leave the SDKs out of sys.modules. The spec's property
    # is that the MODULE itself imports no vendor SDK: load its file
    # standalone in an isolated (-I) interpreter and check sys.modules.
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('pms_catalogue_under_test', {str(MODULE_PATH)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module  # the dataclasses machinery needs it registered\n"
        "spec.loader.exec_module(module)\n"
        "bad = sorted(n for n in ('openai', 'anthropic', 'google.genai', 'ollama') if n in sys.modules)\n"
        "sys.stdout.write(str(bad))\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"vendor SDKs imported: {proc.stdout} {proc.stderr}"


def test_warn_dropped_once(caplog):
    caplog.set_level(logging.WARNING)
    provider = "pms-warn-test-provider"
    warn_dropped("openchat", provider, ["min_p", "ttl"])
    warn_dropped("openchat", provider, ["min_p"])
    min_p_records = [r for r in caplog.records if r.levelno == logging.WARNING and "min_p" in r.getMessage()]
    assert len(min_p_records) == 1
    ttl_records = [r for r in caplog.records if r.levelno == logging.WARNING and "ttl" in r.getMessage()]
    assert len(ttl_records) == 1
