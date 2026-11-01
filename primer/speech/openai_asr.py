"""Speech-to-text adapter over the OpenAI audio transcriptions API.

Same client skeleton as :mod:`primer.llm.openchat`: a lazily built
``AsyncOpenAI`` with the ``no-key-required`` sentinel for unauthenticated
self-hosted endpoints, and an idempotent ``aclose``. The wire shape is
normative (``asr-tts.md`` section 2): the file part is a
``(filename, fileobj, mimetype)`` triple and the timeout is a
connect/read pair, never a scalar.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from openai import AsyncOpenAI

from primer.int.asr import ASR
from primer.model.except_ import ConfigError
from primer.model.provider import (
    SpeechToTextProvider,
    SpeechToTextProviderType,
)
from primer.model.speech import SpeechError, Transcription
from primer.llm._retry import backoff_delay
from primer.speech._errors import is_replayable, speech_error_from_exception


logger = logging.getLogger(__name__)

# Fallbacks when the provider row leaves a timeout unset. Opening a
# transcription request should fail fast; reading the result must not,
# because a cold model load happens on the read side.
_DEFAULT_CONNECT_SECONDS = 5.0
_DEFAULT_READ_SECONDS = 300.0


class OpenAIASR(ASR):
    """Transcription adapter for OpenAI-compatible audio endpoints."""

    def __init__(self, provider: SpeechToTextProvider) -> None:
        if provider.provider != SpeechToTextProviderType.OPENAI:
            raise ConfigError(
                f"OpenAIASR requires provider type openai; got {provider.provider}"
            )
        self._provider = provider
        self._config = provider.config
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        """Construct the AsyncOpenAI client lazily on first use."""
        if self._client is None:
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            ) or "no-key-required"
            self._client = AsyncOpenAI(
                base_url=str(self._config.url),
                api_key=key,
                max_retries=0,  # the row's max_retries is the only policy (W50)
            )
        return self._client

    def _timeout(self) -> httpx.Timeout:
        """Connect/read pair. A scalar would kill a slow transcription
        that is still progressing."""
        limits = self._provider.limits
        connect = limits.connect_timeout_seconds or _DEFAULT_CONNECT_SECONDS
        read = limits.request_timeout_seconds or _DEFAULT_READ_SECONDS
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)

    async def transcribe(
        self,
        *,
        model: str,
        audio: bytes,
        filename: str,
        mimetype: str,
        language: str | None = None,
    ) -> Transcription | SpeechError:
        extra: dict[str, str] = {}
        if language:
            extra["language"] = language
        limits = self._provider.limits
        attempts = limits.max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._get_client().audio.transcriptions.create(
                    model=model,
                    file=(filename, audio, mimetype),
                    response_format="json",
                    timeout=self._timeout(),
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 -- error is a value here
                replayable = attempt < attempts - 1 and is_replayable(exc)
                logger.warning(
                    "ASR transcription failed",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                        "will_retry": replayable,
                    },
                )
                if not replayable:
                    return speech_error_from_exception(exc)
                await asyncio.sleep(backoff_delay(
                    attempt + 1,
                    base=limits.retry_backoff_seconds,
                    cap=limits.retry_backoff_max_seconds,
                ))
                continue
            return Transcription(text=getattr(response, "text", "") or "")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


__all__ = ["OpenAIASR"]
