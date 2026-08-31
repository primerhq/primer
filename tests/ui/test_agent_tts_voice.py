"""The agent editor exposes the per-agent voice override.

Mirrors tests/ui/test_agents_model_profile_field.py: the agent form is
the only place a per-agent knob can be set, so a backend field with no
form control is an incomplete feature.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_agent_form_carries_a_tts_voice_control() -> None:
    src = _read("components/agents.jsx")
    assert "tts_voice" in src
    assert 'data-testid="agent-tts-voice"' in src


def test_the_voice_options_are_enumerated_not_hardcoded() -> None:
    src = _read("components/agents.jsx")
    assert "/audio/voices" in src
    for kokoro_name in ("af_heart", "am_adam", "af_bella"):
        assert kokoro_name not in src


def test_the_control_is_hidden_when_no_tts_provider_is_configured() -> None:
    src = _read("components/agents.jsx")
    assert "tts_configured" in src


def test_the_empty_option_means_use_the_global_default() -> None:
    src = _read("components/agents.jsx")
    voice_at = src.index('data-testid="agent-tts-voice"')
    window_src = src[voice_at: voice_at + 500]
    assert "global default" in window_src.lower()
