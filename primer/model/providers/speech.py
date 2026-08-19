"""Speech (ASR / TTS) provider configuration.

Two provider families on the de-facto standard OpenAI audio API shape
(``POST /v1/audio/transcriptions`` and ``POST /v1/audio/speech``):
:class:`SpeechToTextProvider` and :class:`TextToSpeechProvider`, peers of
the embedding and cross-encoder families. Both carry an
OpenAI-compatible HTTP connection config (:class:`_HttpApiKeyConfig`) and
the mandatory :class:`Limits` block.

Speech deliberately has NO profile rows.
:class:`primer.model.model_profile.ModelProfile` is LLM-only by design
(the profiles router hardcodes an ``LLMProvider`` existence check), so
``default_model`` -- and ``default_voice`` for TTS -- live inline on the
provider row, the shape
:class:`primer.model.providers.embedding.EmbeddingProvider` already uses
for its model list.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from primer.model.common import Identifiable
from primer.model.providers._shared import Limits, _HttpApiKeyConfig


class SpeechToTextProviderType(str, Enum):
    """Supported speech-to-text backends.

    Only the OpenAI audio shape is modelled: every self-hosted ASR server
    worth targeting imitates it, and anything outside that shape
    (ElevenLabs, Deepgram, Azure) needs its own adapter rather than
    another member here.
    """

    OPENAI = "openai"


class TextToSpeechProviderType(str, Enum):
    """Supported text-to-speech backends. See
    :class:`SpeechToTextProviderType` for why there is only one."""

    OPENAI = "openai"


class SpeechToTextConfig(_HttpApiKeyConfig):
    """Connection settings for an OpenAI-compatible transcription endpoint.

    ``url`` is the API base (e.g. ``http://asr.local:8006/v1``), not the
    ``/audio/transcriptions`` path: the openai SDK appends the route.
    """


class TextToSpeechConfig(_HttpApiKeyConfig):
    """Connection settings for an OpenAI-compatible speech endpoint.

    ``url`` is the API base (e.g. ``http://tts.local:8004/v1``).
    """


def _coerce(data: object, enum_cls: type[Enum], config_map: dict) -> object:
    """Parse ``config`` with the concrete class named by ``provider``.

    Same pre-validation pass as
    :meth:`primer.model.providers.llm.LLMProvider._coerce_config_to_provider`:
    the row's ``provider`` field names its config class, so a second
    vendor is added by extending ``config_map`` rather than reshaping the
    ``config`` annotation at every call site.
    """
    if not isinstance(data, dict):
        return data
    config = data.get("config")
    if not isinstance(config, dict):
        return data
    provider = data.get("provider")
    try:
        provider_enum = provider if isinstance(provider, enum_cls) else enum_cls(provider)
    except ValueError:
        return data
    config_cls: type[BaseModel] | None = config_map.get(provider_enum)
    if config_cls is None:
        return data
    return {**data, "config": config_cls.model_validate(config)}


class SpeechToTextProvider(Identifiable):
    """A configured speech-to-text backend."""

    _id_prefix: ClassVar[str] = "stt-provider"

    provider: SpeechToTextProviderType = Field(
        ...,
        description="Which speech-to-text backend this entry targets.",
    )
    default_model: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side transcription model used when a call does not "
            "name one (e.g. 'whisper-1'). Lives inline on the row because "
            "ModelProfile is LLM-only."
        ),
    )
    config: SpeechToTextConfig = Field(
        ...,
        description="Backend-specific connection configuration.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_config_to_provider(cls, data: object) -> object:
        return _coerce(
            data,
            SpeechToTextProviderType,
            {SpeechToTextProviderType.OPENAI: SpeechToTextConfig},
        )


class TextToSpeechProvider(Identifiable):
    """A configured text-to-speech backend."""

    _id_prefix: ClassVar[str] = "tts-provider"

    provider: TextToSpeechProviderType = Field(
        ...,
        description="Which text-to-speech backend this entry targets.",
    )
    default_model: str = Field(
        ...,
        min_length=1,
        description="Provider-side synthesis model (e.g. 'kokoro').",
    )
    default_voice: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side voice name used when neither the agent nor the "
            "active speech config names one. Voice names are NOT portable "
            "across providers, so this is per-row rather than a constant."
        ),
    )
    config: TextToSpeechConfig = Field(
        ...,
        description="Backend-specific connection configuration.",
    )
    limits: Limits = Field(
        ...,
        description="Rate-limit settings enforced when calling this provider.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_config_to_provider(cls, data: object) -> object:
        return _coerce(
            data,
            TextToSpeechProviderType,
            {TextToSpeechProviderType.OPENAI: TextToSpeechConfig},
        )


__all__ = [
    "SpeechToTextConfig",
    "SpeechToTextProvider",
    "SpeechToTextProviderType",
    "TextToSpeechConfig",
    "TextToSpeechProvider",
    "TextToSpeechProviderType",
]
