"""Per-row speech adapter registries.

ONE generic class, instantiated twice: ``app.state.stt_registry`` and
``app.state.tts_registry``. Speech adapters are stateless HTTP clients,
so they are per-class SIBLINGS of
:class:`primer.api.registries.web_search_registry.WebSearchRegistry`
rather than members of :class:`ProviderRegistry`'s model-family
invalidation web -- nothing about a speech row participates in profile
or embedder invalidation.

The get / invalidate / aclose triad, including the race-resilience
pattern (concurrent gets for one id may construct twice but only one wins
the cache; the loser is aclose()'d), is the web-search registry's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from primer.model.except_ import NotFoundError
from primer.model.provider import SpeechToTextProvider, TextToSpeechProvider


if TYPE_CHECKING:
    from primer.int.asr import ASR
    from primer.int.storage import Storage
    from primer.int.tts import TTS


logger = logging.getLogger(__name__)


class SpeechRegistry:
    """Cache + lifecycle for per-row speech adapter instances."""

    def __init__(
        self,
        *,
        storage: "Storage[Any]",
        factory: Callable[[Any], Any],
        label: str = "speech",
    ) -> None:
        self._storage = storage
        self._factory = factory
        self._label = label
        self._instances: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def get(self, provider_id: str) -> Any:
        """Resolve a row to its live adapter instance.

        Cached per id. Storage lookup and construction run OUTSIDE the
        lock so concurrent gets for different ids do not serialise.
        """
        async with self._lock:
            cached = self._instances.get(provider_id)
            if cached is not None:
                return cached

        row = await self._storage.get(provider_id)
        if row is None:
            raise NotFoundError(
                f"{self._label} provider {provider_id!r} does not exist"
            )
        candidate = self._factory(row)

        async with self._lock:
            winner = self._instances.get(provider_id)
            if winner is None:
                self._instances[provider_id] = candidate
                return candidate

        try:
            await candidate.aclose()
        except Exception as exc:  # noqa: BLE001 -- non-fatal
            logger.warning(
                "SpeechRegistry(%s): race-loser aclose failed: %s", self._label, exc,
            )
        return winner

    async def invalidate(self, provider_id: str) -> None:
        """Drop the cached instance for one id and aclose() it."""
        async with self._lock:
            instance = self._instances.pop(provider_id, None)
        if instance is not None:
            try:
                await instance.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SpeechRegistry(%s).invalidate: aclose failed: %s",
                    self._label,
                    exc,
                )

    async def aclose(self) -> None:
        """Drop and aclose every cached instance."""
        async with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            try:
                await instance.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SpeechRegistry(%s).aclose: instance close failed: %s",
                    self._label,
                    exc,
                )


def default_stt_factory(provider: SpeechToTextProvider) -> "ASR":
    """Construct the ASR adapter for a speech-to-text row."""
    from primer.speech.openai_asr import OpenAIASR

    return OpenAIASR(provider)


def default_tts_factory(provider: TextToSpeechProvider) -> "TTS":
    """Construct the TTS adapter for a text-to-speech row."""
    from primer.speech.openai_tts import OpenAITTS

    return OpenAITTS(provider)


__all__ = ["SpeechRegistry", "default_stt_factory", "default_tts_factory"]
