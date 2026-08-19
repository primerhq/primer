"""Text-to-speech adapter over the OpenAI audio speech API.

The streaming discipline is the whole point of this module. The SDK's
``with_streaming_response`` opens the response without reading it, and
this adapter yields each chunk THROUGH as it arrives. Collecting the
chunks and returning the join reintroduces the full synthesis wait while
looking like streaming; ``tests/speech/test_openai_tts.py`` pins the
cadence so that regression fails loudly.

Retries follow ``asr-tts.md`` section 5: a failed CONNECT is safe to
replay, a half-consumed audio stream is not, so a retry only happens
when zero bytes have been yielded (Task 8 extends the loop).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator

import httpx
from openai import APIConnectionError, AsyncOpenAI

from primer.int.tts import TTS
from primer.model.except_ import ConfigError
from primer.model.provider import (
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from primer.model.speech import SpeechError
from primer.speech._errors import speech_error_from_exception


logger = logging.getLogger(__name__)

_DEFAULT_CONNECT_SECONDS = 5.0
_DEFAULT_READ_SECONDS = 300.0
# Bytes pulled per iteration. The read timeout applies BETWEEN chunks, so
# a small size keeps time-to-first-audio low without risking a stall.
_CHUNK_BYTES = 4096


class OpenAITTS(TTS):
    """Streaming synthesis adapter for OpenAI-compatible audio endpoints."""

    def __init__(self, provider: TextToSpeechProvider) -> None:
        if provider.provider != TextToSpeechProviderType.OPENAI:
            raise ConfigError(
                f"OpenAITTS requires provider type openai; got {provider.provider}"
            )
        self._provider = provider
        self._config = provider.config
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            ) or "no-key-required"
            self._client = AsyncOpenAI(
                base_url=str(self._config.url),
                api_key=key,
            )
        return self._client

    def _timeout(self) -> httpx.Timeout:
        """Connect/read pair. With streaming, ``read`` bounds the gap
        BETWEEN chunks, not the whole response."""
        limits = self._provider.limits
        connect = limits.connect_timeout_seconds or _DEFAULT_CONNECT_SECONDS
        read = limits.request_timeout_seconds or _DEFAULT_READ_SECONDS
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)

    async def stream(
        self,
        *,
        model: str,
        text: str,
        voice: str,
        response_format: str = "mp3",
    ) -> AsyncIterator[bytes | SpeechError]:
        limits = self._provider.limits
        attempts = limits.max_retries + 1
        for attempt in range(attempts):
            yielded = 0
            try:
                async with self._get_client().audio.speech.with_streaming_response.create(
                    model=model,
                    voice=voice,
                    input=text,
                    response_format=response_format,
                    timeout=self._timeout(),
                ) as response:
                    async for chunk in response.iter_bytes(_CHUNK_BYTES):
                        if chunk:
                            yielded += len(chunk)
                            yield chunk
                return
            except Exception as exc:  # noqa: BLE001 -- error is a value here
                # A failed CONNECT is safe to replay. A half-consumed
                # audio stream is not: replaying it would concatenate two
                # partial syntheses into one incoherent clip.
                replayable = (
                    yielded == 0
                    and attempt < attempts - 1
                    and isinstance(exc, APIConnectionError)
                )
                logger.warning(
                    "TTS synthesis failed",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                        "bytes_yielded": yielded,
                        "will_retry": replayable,
                    },
                )
                if not replayable:
                    yield speech_error_from_exception(exc)
                    return
                await asyncio.sleep(
                    random.uniform(
                        0.0,
                        min(
                            limits.retry_backoff_seconds * (2 ** attempt),
                            limits.retry_backoff_max_seconds,
                        ),
                    )
                )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


__all__ = ["OpenAITTS"]
