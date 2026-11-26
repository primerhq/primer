"""The speech adapters hold a RateLimiter slot for the whole call, the way the LLM adapters do (2026-11-14 item)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.model.provider import (
    Limits,
    SpeechToTextProvider,
    SpeechToTextProviderType,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from primer.speech.openai_asr import OpenAIASR
from primer.speech.openai_tts import OpenAITTS


def _stt_row() -> SpeechToTextProvider:
    return SpeechToTextProvider(
        id="stt-a",
        provider=SpeechToTextProviderType.OPENAI,
        default_model="whisper-1",
        config={"url": "http://asr.local:8006/v1"},
        limits=Limits(
            max_concurrency=1,
            connect_timeout_seconds=5.0,
            request_timeout_seconds=120.0,
        ),
    )


def _tts_row() -> TextToSpeechProvider:
    return TextToSpeechProvider(
        id="tts-a",
        provider=TextToSpeechProviderType.OPENAI,
        default_model="kokoro",
        default_voice="af_heart",
        config={"url": "http://tts.local:8004/v1"},
        limits=Limits(
            max_concurrency=1,
            connect_timeout_seconds=5.0,
            request_timeout_seconds=300.0,
            max_retries=0,
        ),
    )


class _Transcriptions:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_seen = 0
        self.hold = asyncio.Event()

    async def create(self, **kwargs):
        self.in_flight += 1
        self.max_seen = max(self.max_seen, self.in_flight)
        await self.hold.wait()
        self.in_flight -= 1
        return SimpleNamespace(text="ok")


@pytest.mark.asyncio
async def test_two_transcriptions_run_one_at_a_time(monkeypatch):
    limiter = InMemoryRateLimiter()
    calls = _Transcriptions()
    adapter = OpenAIASR(_stt_row(), rate_limiter=limiter)
    monkeypatch.setattr(
        adapter, "_get_client",
        lambda: SimpleNamespace(audio=SimpleNamespace(transcriptions=calls)),
    )
    assert adapter._rate_limit_key == "stt:stt-a"
    first = asyncio.create_task(adapter.transcribe(
        model="m", audio=b"x", filename="f", mimetype="m/a",
    ))
    await asyncio.sleep(0.05)
    assert calls.in_flight == 1
    second = asyncio.create_task(adapter.transcribe(
        model="m", audio=b"x", filename="f", mimetype="m/a",
    ))
    await asyncio.sleep(0.05)
    assert calls.in_flight == 1  # the second call is still waiting for the slot
    calls.hold.set()
    out = await asyncio.gather(first, second)
    assert [o.text for o in out] == ["ok", "ok"]
    assert calls.max_seen == 1  # max_concurrency=1 held


class _Speech:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_seen = 0
        self.hold = asyncio.Event()
        self.with_streaming_response = _Streaming(self)


class _Streaming:
    def __init__(self, speech: _Speech) -> None:
        self._speech = speech

    def create(self, **kwargs):
        return self

    async def __aenter__(self):
        self._speech.in_flight += 1
        self._speech.max_seen = max(self._speech.max_seen, self._speech.in_flight)
        await self._speech.hold.wait()
        return self

    async def __aexit__(self, *exc):
        self._speech.in_flight -= 1
        return False

    async def iter_bytes(self, chunk_size=None):
        yield b"aa"
        yield b"bb"


@pytest.mark.asyncio
async def test_two_syntheses_run_one_at_a_time(monkeypatch):
    limiter = InMemoryRateLimiter()
    speech = _Speech()
    adapter = OpenAITTS(_tts_row(), rate_limiter=limiter)
    monkeypatch.setattr(
        adapter, "_get_client",
        lambda: SimpleNamespace(audio=SimpleNamespace(speech=speech)),
    )
    assert adapter._rate_limit_key == "tts:tts-a"

    async def collect():
        return [c async for c in adapter.stream(model="m", text="hi", voice="v")]

    first = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    assert speech.in_flight == 1
    second = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    assert speech.in_flight == 1  # the second synthesis is still waiting for the slot
    speech.hold.set()
    out = await asyncio.gather(first, second)
    assert out == [[b"aa", b"bb"], [b"aa", b"bb"]]
    assert speech.max_seen == 1  # max_concurrency=1 held
