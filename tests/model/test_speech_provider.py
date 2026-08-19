"""SpeechToTextProvider / TextToSpeechProvider row shape (S4 P1 Task 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.providers._shared import Limits
from primer.model.providers.speech import (
    SpeechToTextConfig,
    SpeechToTextProvider,
    SpeechToTextProviderType,
    TextToSpeechConfig,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)


def _limits() -> Limits:
    return Limits(max_concurrency=2)


def test_stt_row_autogenerates_its_id_prefix() -> None:
    row = SpeechToTextProvider(
        provider=SpeechToTextProviderType.OPENAI,
        default_model="whisper-1",
        config={"url": "http://asr.local:8006/v1"},
        limits=_limits(),
    )
    assert row.id.startswith("stt-provider-")


def test_tts_row_autogenerates_its_id_prefix() -> None:
    row = TextToSpeechProvider(
        provider=TextToSpeechProviderType.OPENAI,
        default_model="kokoro",
        default_voice="af_heart",
        config={"url": "http://tts.local:8004/v1"},
        limits=_limits(),
    )
    assert row.id.startswith("tts-provider-")


def test_config_dict_is_coerced_to_the_concrete_config_class() -> None:
    row = SpeechToTextProvider(
        provider="openai",
        default_model="whisper-1",
        config={"url": "http://asr.local:8006/v1", "api_key": "sk-x"},
        limits=_limits(),
    )
    assert isinstance(row.config, SpeechToTextConfig)
    assert row.config.api_key.get_secret_value() == "sk-x"


def test_tts_config_dict_is_coerced_to_the_concrete_config_class() -> None:
    row = TextToSpeechProvider(
        provider="openai",
        default_model="kokoro",
        default_voice="af_heart",
        config={"url": "http://tts.local:8004/v1"},
        limits=_limits(),
    )
    assert isinstance(row.config, TextToSpeechConfig)


def test_api_key_is_optional_for_self_hosted_endpoints() -> None:
    row = SpeechToTextProvider(
        provider="openai",
        default_model="whisper-1",
        config={"url": "http://asr.local:8006/v1"},
        limits=_limits(),
    )
    assert row.config.api_key is None


def test_unknown_provider_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SpeechToTextProvider(
            provider="deepgram",
            default_model="nova-2",
            config={"url": "http://x.local/v1"},
            limits=_limits(),
        )


def test_default_model_is_required_and_non_empty() -> None:
    with pytest.raises(ValidationError):
        SpeechToTextProvider(
            provider="openai",
            default_model="",
            config={"url": "http://asr.local:8006/v1"},
            limits=_limits(),
        )


def test_tts_default_voice_is_required() -> None:
    with pytest.raises(ValidationError):
        TextToSpeechProvider(
            provider="openai",
            default_model="kokoro",
            config={"url": "http://tts.local:8004/v1"},
            limits=_limits(),
        )


def test_limits_are_required_on_both_families() -> None:
    with pytest.raises(ValidationError):
        SpeechToTextProvider(
            provider="openai",
            default_model="whisper-1",
            config={"url": "http://asr.local:8006/v1"},
        )
    with pytest.raises(ValidationError):
        TextToSpeechProvider(
            provider="openai",
            default_model="kokoro",
            default_voice="af_heart",
            config={"url": "http://tts.local:8004/v1"},
        )

