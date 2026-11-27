"""Adapters send exactly what their wire's catalogue names (PMS Task 2).

Spec: docs/superpowers/specs/2026-11-26-profile-model-settings-design.md,
sections 3 and 5. Task 1's catalogue names the accepted keys per wire; this
pins that every adapter accepts exactly those keys (dropping the rest with a
WARNING that names them) and places each key on the wire where its
setting's wire_path says (SDK keyword vs extra_body vs options vs the
nested dicts the adapter already builds).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

import pytest

from primer.llm.anthropic import _extract_extended_kwargs as _anthropic_extract
from primer.llm.claude_code import ClaudeCodeLLM
from primer.llm.gemini import _extract_extended_kwargs as _gemini_extract
from primer.llm.openchat import _extract_extended_kwargs as _openchat_extract
from primer.llm.openresponses import _extract_extended_kwargs as _openresponses_extract
from primer.llm.openrouter import _extract_extended_kwargs as _openrouter_extract
from primer.llm.ollama import _build_options_and_kwargs
from primer.llm.settings_catalogue import Setting, catalogue_for
from primer.model.chat import Done, Message, TextPart
from primer.model.provider import (
    ClaudeCodeConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OpenChatFlavor,
)

from tests.llm.claude_code.fake_claude import fake_claude  # noqa: F401  (fixture)

# The wires with an adapter-level extractor; claude_code drives the CLI
# instead and is covered by test_claude_code_warns_on_unknown.
EXTRACTABLE_WIRES = [
    "openchat/openai",
    "openchat/vllm",
    "openchat/lmstudio",
    "openchat/ollama",
    "openchat/other",
    "openresponses/openai",
    "openresponses/lmstudio",
    "openresponses/vllm",
    "openresponses/other",
    "openrouter",
    "anthropic",
    "gemini",
    "ollama",
]


def _valid_value(setting: Setting):
    """One value of the setting's declared shape (the catalogue is the source)."""
    if setting.type == "enum":
        return setting.values[0]
    if setting.type == "integer":
        return int(setting.min) if setting.min is not None else 1
    if setting.type == "number":
        return 0.5
    if setting.type == "boolean":
        return True
    if setting.type == "string":
        return "x"
    return {}  # object: an empty dict is valid for open, closed and opaque shapes


def _extract(wire: str, extended: dict, provider_id: str):
    """The adapter-level extractor for one wire."""
    cat = catalogue_for(wire)
    if wire.startswith("openchat/"):
        flavor = OpenChatFlavor(wire.rsplit("/", 1)[1])
        return _openchat_extract(extended, catalogue=cat, flavor=flavor, provider_id=provider_id)
    if wire.startswith("openresponses/"):
        return _openresponses_extract(extended, catalogue=cat, provider_id=provider_id)
    if wire == "openrouter":
        return _openrouter_extract(extended, catalogue=cat, provider_id=provider_id)
    if wire == "anthropic":
        return _anthropic_extract(extended, catalogue=cat, provider_id=provider_id)
    if wire == "gemini":
        return _gemini_extract(extended, catalogue=cat, provider_id=provider_id)
    return _build_options_and_kwargs(
        temperature=None,
        top_p=None,
        max_output_tokens=None,
        stop=None,
        extended=extended,
        catalogue=cat,
        provider_id=provider_id,
    )


def _dropped_keys(caplog: pytest.LogCaptureFixture, provider_id: str) -> set[str]:
    """The extended keys warn_dropped named for one provider (its message names them)."""
    keys: set[str] = set()
    for record in caplog.records:
        if record.levelno != logging.WARNING:
            continue
        message = record.getMessage()
        if provider_id not in message or not message.startswith("dropping extended key "):
            continue
        keys.add(message[len("dropping extended key "):].split(" for provider ", 1)[0])
    return keys


def _contains(out, key: str) -> bool:
    if isinstance(out, tuple):
        return any(key in part for part in out if isinstance(part, dict))
    return key in out or key in out.get("extra_body", {})


@pytest.mark.parametrize("wire", EXTRACTABLE_WIRES)
def test_adapter_accepts_exactly_its_catalogue(wire: str, caplog: pytest.LogCaptureFixture) -> None:
    """Every catalogue key lands somewhere (nothing dropped, no WARNING); a key
    outside the catalogue is dropped with a WARNING naming it."""
    caplog.set_level(logging.WARNING)
    cat = catalogue_for(wire)

    accept_id = f"t2-accept-{uuid4().hex}"
    extended = {s.key: _valid_value(s) for s in cat.settings}
    if cat.body_passthrough:
        extended["body"] = {"ping": 1}
    _extract(wire, extended, accept_id)
    # The gemini fold keeps the level and drops the budget when both are set
    expected_drops = {"thinking_budget"} if wire == "gemini" else set()
    assert _dropped_keys(caplog, accept_id) == expected_drops

    unknown_id = f"t2-unknown-{uuid4().hex}"
    out = _extract(wire, {"frobnicate": 1}, unknown_id)
    assert _dropped_keys(caplog, unknown_id) == {"frobnicate"}
    assert not _contains(out, "frobnicate")


def test_openchat_openai_sdk_keywords() -> None:
    out = _openchat_extract(
        {"verbosity": "high", "service_tier": "flex", "prompt_cache_key": "k"},
        catalogue=catalogue_for("openchat/openai"),
        flavor=OpenChatFlavor.OPENAI,
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out == {"verbosity": "high", "service_tier": "flex", "prompt_cache_key": "k"}


def test_openresponses_verbosity_context_and_mode_placements() -> None:
    out = _openresponses_extract(
        {"verbosity": "high", "reasoning_context": "all_turns", "reasoning_mode": "pro"},
        catalogue=catalogue_for("openresponses/openai"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["text"] == {"verbosity": "high"}
    assert out["reasoning"] == {"context": "all_turns", "mode": "pro"}


def test_openchat_vllm_sampling_extras_in_extra_body() -> None:
    out = _openchat_extract(
        {"top_k": 5, "min_p": 0.2, "thinking_token_budget": 100},
        catalogue=catalogue_for("openchat/vllm"),
        flavor=OpenChatFlavor.VLLM,
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["extra_body"] == {"top_k": 5, "min_p": 0.2, "thinking_token_budget": 100}


def test_openchat_lmstudio_extras_in_extra_body() -> None:
    out = _openchat_extract(
        {"ttl": 60.0, "draft_model": "draft-1"},
        catalogue=catalogue_for("openchat/lmstudio"),
        flavor=OpenChatFlavor.LMSTUDIO,
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["extra_body"] == {"ttl": 60.0, "draft_model": "draft-1"}


def test_openrouter_provider_in_extra_body() -> None:
    out = _openrouter_extract(
        {"provider": {"order": ["anthropic"]}},
        catalogue=catalogue_for("openrouter"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["extra_body"] == {"provider": {"order": ["anthropic"]}}


def test_anthropic_speed_and_inference_geo_are_sdk_keywords() -> None:
    out = _anthropic_extract(
        {"speed": "fast", "inference_geo": "us-west"},
        catalogue=catalogue_for("anthropic"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["speed"] == "fast"
    assert out["inference_geo"] == "us-west"
    assert out["extra_headers"] == {"anthropic-beta": "fast-mode-2026-02-01"}


def test_anthropic_speed_omitted_sends_no_header() -> None:
    out = _anthropic_extract(
        {"service_tier": "standard_only"},
        catalogue=catalogue_for("anthropic"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert "extra_headers" not in out


def test_gemini_media_resolution_and_service_tier_reach_config() -> None:
    out = _gemini_extract(
        {"media_resolution": "4k", "service_tier": "auto"},
        catalogue=catalogue_for("gemini"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["media_resolution"] == "4k"
    assert out["service_tier"] == "auto"


def test_ollama_min_p_and_repeat_last_n_in_options() -> None:
    options, top = _build_options_and_kwargs(
        temperature=None,
        top_p=None,
        max_output_tokens=None,
        stop=None,
        extended={"min_p": 0.1, "repeat_last_n": 10},
        catalogue=catalogue_for("ollama"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert options == {"min_p": 0.1, "repeat_last_n": 10}
    assert top == {}


def test_body_merges_into_extra_body() -> None:
    out = _openchat_extract(
        {"body": {"return_progress": True}, "top_k": 5},
        catalogue=catalogue_for("openchat/vllm"),
        flavor=OpenChatFlavor.VLLM,
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["extra_body"] == {"return_progress": True, "top_k": 5}
    out = _openresponses_extract(
        {"body": {"return_progress": True}},
        catalogue=catalogue_for("openresponses/vllm"),
        provider_id=f"t2-{uuid4().hex}",
    )
    assert out["extra_body"] == {"return_progress": True}


def test_body_reserved_fields_are_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    provider_id = f"t2-reserved-{uuid4().hex}"
    out = _openchat_extract(
        {"body": {"model": "evil", "stream": False}},
        catalogue=catalogue_for("openchat/vllm"),
        flavor=OpenChatFlavor.VLLM,
        provider_id=provider_id,
    )
    assert out == {}
    assert _dropped_keys(caplog, provider_id) == {"body.model", "body.stream"}


def test_body_on_a_non_passthrough_wire_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    provider_id = f"t2-body-nopassthrough-{uuid4().hex}"
    out = _openchat_extract(
        {"body": {"return_progress": True}},
        catalogue=catalogue_for("openchat/openai"),
        flavor=OpenChatFlavor.OPENAI,
        provider_id=provider_id,
    )
    assert out == {}
    assert _dropped_keys(caplog, provider_id) == {"body"}


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_code"


def _cc_provider(binary: str) -> LLMProvider:
    return LLMProvider(
        id="cc-t2",
        provider=LLMProviderType.CLAUDE_CODE,
        config=ClaudeCodeConfig(
            binary_path=binary,
            pass_env=["FAKE_CLAUDE_FIXTURE", "FAKE_CLAUDE_RECORD"],
        ),
        limits=Limits(max_concurrency=2),
    )


async def test_claude_code_warns_on_unknown(
    fake_claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(FIXTURES / "local_command_reply.ndjson"))
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    llm = ClaudeCodeLLM(_cc_provider(fake_claude))
    events = [
        e
        async for e in llm.stream(
            model="sonnet",
            messages=[Message(role="user", parts=[TextPart(text="hi")])],
            extended={"effort": "high", "foo": 1},
        )
    ]
    assert isinstance(events[-1], Done)
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec["argv"][rec["argv"].index("--effort") + 1] == "high"
    assert _dropped_keys(caplog, "cc-t2") == {"foo"}
