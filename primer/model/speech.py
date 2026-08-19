"""Speech subsystem models: interface value types + the active-speech row.

:class:`ActiveSpeechConfig` is the install-wide "which speech providers
are the defaults" state, mirroring
:class:`primer.model.web_search.ActiveWebSearchConfig`: one reserved,
underscore-prefixed row id that is not exposed through CRUD. Speech has
no single/aggregated mode axis, so the three settings sit directly on the
row instead of inside a discriminated ``config`` union.

:class:`Transcription` and :class:`SpeechError` are the values the
:class:`primer.int.asr.ASR` and :class:`primer.int.tts.TTS` ABCs exchange
with their callers. They follow the LLM adapters' error contract: an
adapter reports provider failure by RETURNING (or yielding) a
:class:`SpeechError` rather than raising, so consumers can rely on the
call always completing cleanly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from primer.model.common import Identifiable


# Reserved row id of the singleton active-speech row. Underscore-prefixed
# because it is a non-CRUD-exposed singleton (matches
# ``_active_web_search_config``).
ACTIVE_SPEECH_CONFIG_ID = "_active_speech_config"

# Error code for "the provider is up but its model is still loading".
# ASR services return 503 for roughly the first ten seconds after start
# and vLLM-class services can take minutes, so this is a retriable state
# rather than a failure.
WARMING_UP_CODE = "warming_up"


class Transcription(BaseModel):
    """One transcription result. ``text`` is the whole record; audio is
    ephemeral and never persisted."""

    text: str = Field(default="", description="Transcribed text.")


class SpeechError(BaseModel):
    """Terminal error value returned or yielded by a speech adapter."""

    code: str = Field(..., description="Stable machine-readable error code.")
    message: str = Field(..., description="Human-readable detail.")
    retriable: bool = Field(
        default=False,
        description=(
            "True when the caller should back off and try again, e.g. the "
            "provider is still loading its model."
        ),
    )


class ActiveSpeechConfig(Identifiable):
    """Singleton row at id :data:`ACTIVE_SPEECH_CONFIG_ID`.

    ``GET /v1/speech_active_config`` reads it and ``PUT`` replaces it.
    Every field is optional: an install with no speech providers is a
    normal steady state, not a broken bootstrap, so the GET synthesises
    an empty row rather than reporting 503.
    """

    stt_provider_id: str | None = Field(
        default=None,
        description="Default SpeechToTextProvider id, or None when unset.",
    )
    tts_provider_id: str | None = Field(
        default=None,
        description="Default TextToSpeechProvider id, or None when unset.",
    )
    tts_voice: str | None = Field(
        default=None,
        description=(
            "Install-wide default voice. Overridden per agent by "
            "``Agent.tts_voice``; falls back to the provider row's "
            "``default_voice`` when unset."
        ),
    )


__all__ = [
    "ACTIVE_SPEECH_CONFIG_ID",
    "ActiveSpeechConfig",
    "SpeechError",
    "Transcription",
    "WARMING_UP_CODE",
]
