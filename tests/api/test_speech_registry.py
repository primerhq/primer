"""SpeechRegistry get/invalidate/aclose triad (S4 P1 Task 9)."""

from __future__ import annotations

import pytest

from primer.api.registries.speech_registry import (
    SpeechRegistry,
    default_stt_factory,
    default_tts_factory,
)
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    Limits,
    SpeechToTextProvider,
    SpeechToTextProviderType,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from tests.conftest import _InMemoryStorage


class _Adapter:
    def __init__(self, row) -> None:
        self.row = row
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _stt_row(row_id: str = "stt-a") -> SpeechToTextProvider:
    return SpeechToTextProvider(
        id=row_id,
        provider=SpeechToTextProviderType.OPENAI,
        default_model="whisper-1",
        config={"url": "http://asr.local:8006/v1"},
        limits=Limits(max_concurrency=1),
    )


def _tts_row(row_id: str = "tts-a") -> TextToSpeechProvider:
    return TextToSpeechProvider(
        id=row_id,
        provider=TextToSpeechProviderType.OPENAI,
        default_model="kokoro",
        default_voice="af_heart",
        config={"url": "http://tts.local:8004/v1"},
        limits=Limits(max_concurrency=1),
    )


@pytest.mark.asyncio
async def test_get_caches_one_adapter_per_row_id() -> None:
    storage = _InMemoryStorage(SpeechToTextProvider)
    await storage.create(_stt_row())
    registry = SpeechRegistry(storage=storage, factory=_Adapter)

    first = await registry.get("stt-a")
    second = await registry.get("stt-a")
    assert first is second


@pytest.mark.asyncio
async def test_get_on_a_missing_row_raises_not_found() -> None:
    storage = _InMemoryStorage(SpeechToTextProvider)
    registry = SpeechRegistry(storage=storage, factory=_Adapter)
    with pytest.raises(NotFoundError):
        await registry.get("nope")


@pytest.mark.asyncio
async def test_invalidate_drops_and_closes_the_cached_adapter() -> None:
    storage = _InMemoryStorage(SpeechToTextProvider)
    await storage.create(_stt_row())
    registry = SpeechRegistry(storage=storage, factory=_Adapter)

    first = await registry.get("stt-a")
    await registry.invalidate("stt-a")
    assert first.closed == 1
    second = await registry.get("stt-a")
    assert second is not first


@pytest.mark.asyncio
async def test_aclose_closes_every_cached_adapter() -> None:
    storage = _InMemoryStorage(SpeechToTextProvider)
    await storage.create(_stt_row("a"))
    await storage.create(_stt_row("b"))
    registry = SpeechRegistry(storage=storage, factory=_Adapter)

    first = await registry.get("a")
    second = await registry.get("b")
    await registry.aclose()
    assert (first.closed, second.closed) == (1, 1)
    assert registry._instances == {}


@pytest.mark.asyncio
async def test_invalidating_an_uncached_id_is_a_no_op() -> None:
    storage = _InMemoryStorage(SpeechToTextProvider)
    registry = SpeechRegistry(storage=storage, factory=_Adapter)
    await registry.invalidate("never-cached")


def test_the_default_factories_build_the_right_adapters() -> None:
    from primer.speech.openai_asr import OpenAIASR
    from primer.speech.openai_tts import OpenAITTS

    assert isinstance(default_stt_factory(_stt_row()), OpenAIASR)
    assert isinstance(default_tts_factory(_tts_row()), OpenAITTS)
