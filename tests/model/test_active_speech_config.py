"""ActiveSpeechConfig singleton + speech value types (S4 P1 Task 3)."""

from __future__ import annotations

from primer.model.speech import (
    ACTIVE_SPEECH_CONFIG_ID,
    ActiveSpeechConfig,
    SpeechError,
    Transcription,
    WARMING_UP_CODE,
)


def test_singleton_id_is_underscore_prefixed() -> None:
    assert ACTIVE_SPEECH_CONFIG_ID == "_active_speech_config"


def test_every_field_is_optional_so_an_unconfigured_install_is_valid() -> None:
    row = ActiveSpeechConfig(id=ACTIVE_SPEECH_CONFIG_ID)
    assert row.stt_provider_id is None
    assert row.tts_provider_id is None
    assert row.tts_voice is None


def test_the_row_round_trips_its_three_fields() -> None:
    row = ActiveSpeechConfig(
        id=ACTIVE_SPEECH_CONFIG_ID,
        stt_provider_id="stt-a",
        tts_provider_id="tts-a",
        tts_voice="af_heart",
    )
    assert row.model_dump() == {
        "id": ACTIVE_SPEECH_CONFIG_ID,
        "stt_provider_id": "stt-a",
        "tts_provider_id": "tts-a",
        "tts_voice": "af_heart",
    }


def test_transcription_defaults_to_empty_text() -> None:
    assert Transcription().text == ""


def test_speech_error_defaults_to_non_retriable() -> None:
    err = SpeechError(code="provider_error", message="boom")
    assert err.retriable is False


def test_warming_up_code_is_the_retriable_sentinel() -> None:
    err = SpeechError(code=WARMING_UP_CODE, message="loading", retriable=True)
    assert err.code == "warming_up"
    assert err.retriable is True
