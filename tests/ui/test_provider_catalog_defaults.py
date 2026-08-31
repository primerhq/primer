"""M11c: active defaults are managed IN the catalog, not a settings page.

The voice picker is fed by the GET /v1/audio/voices passthrough, never a
hardcoded list: af_heart is a Kokoro name and means nothing to OpenAI.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_catalog_reads_and_writes_the_active_speech_config() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "/speech_active_config" in src
    assert "PC_ActiveSpeechPanel" in src


def test_the_catalog_reads_and_writes_the_active_web_search_config() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "/web_search_active_config" in src
    assert "PC_ActiveWebSearchPanel" in src


def test_the_voice_picker_is_fed_by_the_voices_passthrough() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "/audio/voices" in src


def test_no_voice_name_is_hardcoded_in_the_console() -> None:
    src = _read("components/provider-catalog.jsx")
    for kokoro_name in ("af_heart", "am_adam", "af_bella"):
        assert kokoro_name not in src, (
            f"{kokoro_name} is a Kokoro voice name; enumerate voices instead"
        )


def test_the_speech_defaults_panel_covers_all_three_settings() -> None:
    src = _read("components/provider-catalog.jsx")
    for field in ("stt_provider_id", "tts_provider_id", "tts_voice"):
        assert field in src, f"the active-speech panel does not edit {field}"


def test_the_speech_panel_renders_on_both_speech_classes() -> None:
    src = _read("components/provider-catalog.jsx")
    assert 'klass.key === "stt" || klass.key === "tts"' in src


def test_the_defaults_panels_carry_test_ids() -> None:
    src = _read("components/provider-catalog.jsx")
    assert 'data-testid="active-speech-config"' in src
    assert 'data-testid="active-web-search-config"' in src


def test_the_web_search_panel_edits_both_config_modes() -> None:
    """ActiveWebSearchConfig is a discriminated union of single and
    aggregated; a panel that only writes single silently downgrades an
    aggregated install the first time an operator touches it."""
    src = _read("components/provider-catalog.jsx")
    assert '"aggregated"' in src
    assert "provider_ids" in src
    assert 'data-testid="active-web-search-mode"' in src


def test_the_aggregated_branch_sends_a_list_not_a_single_id() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "provider_ids: " in src
