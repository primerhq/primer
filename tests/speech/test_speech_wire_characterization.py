"""ASR and TTS through the real openai SDK with an in-process transport (Part 2 T50, W50).

Characterization: what a successful call sends and returns, and the HTTP attempts the default policy (``max_retries: 2``)
makes for a transient status that recovers, a status that does not, and a rejection. These hold before and after the SDK's
own retries are switched off, because the row's loop then makes the replays the SDK used to make.
"""

from __future__ import annotations

import importlib

import pytest
from openai import AsyncOpenAI

import primer.speech.openai_asr as asr_mod
import primer.speech.openai_tts as tts_mod
from primer.model.provider import (
    Limits,
    SpeechToTextProvider,
    SpeechToTextProviderType,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from primer.model.speech import SpeechError, Transcription, WARMING_UP_CODE

_HTTP = next(
    importlib.import_module(cls.__module__.split(".")[0])
    for cls in type(AsyncOpenAI(api_key="probe")._client).__mro__
    if cls.__module__.split(".")[0] in ("httpx", "httpx2")
)
# A status the SDK may replay on its own: the header keeps its wait at 1 ms.
_FAST = {"retry-after-ms": "1", "retry-after": "0"}


class _Wire:
    def __init__(self, replies) -> None:
        self.replies = replies
        self.requests: list = []

    def handle(self, request):
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        return _HTTP.Response(status, content=body, headers=headers)


def _route(monkeypatch, module, replies) -> _Wire:
    wire = _Wire(replies)
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kw: AsyncOpenAI(
        **kw, http_client=_HTTP.AsyncClient(transport=_HTTP.MockTransport(wire.handle))))
    return wire


def _limits(**overrides) -> Limits:
    return Limits(**{"max_concurrency": 1, "retry_backoff_seconds": 0.0, "retry_backoff_max_seconds": 0.0, **overrides})


def _asr(**limits) -> asr_mod.OpenAIASR:
    return asr_mod.OpenAIASR(SpeechToTextProvider(
        id="stt", provider=SpeechToTextProviderType.OPENAI, default_model="whisper-1",
        config={"url": "http://asr.test/v1"}, limits=_limits(**limits),
    ))


def _tts(**limits) -> tts_mod.OpenAITTS:
    return tts_mod.OpenAITTS(TextToSpeechProvider(
        id="tts", provider=TextToSpeechProviderType.OPENAI, default_model="kokoro", default_voice="af_heart",
        config={"url": "http://tts.test/v1"}, limits=_limits(**limits),
    ))


async def _transcribe(adapter):
    try:
        return await adapter.transcribe(model="whisper-1", audio=b"RIFF", filename="a.wav", mimetype="audio/wav")
    finally:
        await adapter.aclose()


async def _speak(adapter) -> list:
    try:
        return [item async for item in adapter.stream(model="kokoro", text="hello", voice="af_heart")]
    finally:
        await adapter.aclose()


_TEXT = (200, b'{"text": "hello there"}', {"content-type": "application/json"})
_AUDIO = (200, b"ID3" + b"x" * 5000, {"content-type": "audio/mpeg"})


class TestASR:
    async def test_success(self, monkeypatch) -> None:
        wire = _route(monkeypatch, asr_mod, [_TEXT])
        assert await _transcribe(_asr()) == Transcription(text="hello there")
        assert len(wire.requests) == 1
        assert str(wire.requests[0].url) == "http://asr.test/v1/audio/transcriptions"
        assert b'filename="a.wav"' in wire.requests[0].content

    @pytest.mark.parametrize("status", [500, 502, 429])
    async def test_a_transient_status_that_recovers_takes_two_attempts(self, monkeypatch, status) -> None:
        wire = _route(monkeypatch, asr_mod, [(status, b'{"error": {"message": "x"}}', _FAST), _TEXT])
        assert await _transcribe(_asr()) == Transcription(text="hello there")
        assert len(wire.requests) == 2

    async def test_a_503_that_persists_is_warming_up_after_three_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, asr_mod, [(503, b'{"error": "model still loading"}', _FAST)])
        out = await _transcribe(_asr())
        assert isinstance(out, SpeechError) and out.code == WARMING_UP_CODE and out.retriable
        assert len(wire.requests) == 3

    async def test_a_rejection_is_one_attempt(self, monkeypatch) -> None:
        wire = _route(monkeypatch, asr_mod, [(400, b'{"error": {"message": "bad audio"}}', {})])
        out = await _transcribe(_asr())
        assert isinstance(out, SpeechError) and not out.retriable
        assert len(wire.requests) == 1


class TestTTS:
    async def test_success_streams_the_body_through(self, monkeypatch) -> None:
        wire = _route(monkeypatch, tts_mod, [_AUDIO])
        got = await _speak(_tts())
        assert b"".join(got) == _AUDIO[1]
        assert len(wire.requests) == 1
        assert str(wire.requests[0].url) == "http://tts.test/v1/audio/speech"

    @pytest.mark.parametrize("status", [500, 502, 429])
    async def test_a_transient_status_that_recovers_takes_two_attempts(self, monkeypatch, status) -> None:
        wire = _route(monkeypatch, tts_mod, [(status, b'{"error": {"message": "x"}}', _FAST), _AUDIO])
        assert b"".join(await _speak(_tts())) == _AUDIO[1]
        assert len(wire.requests) == 2

    async def test_a_503_that_persists_is_warming_up_after_three_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, tts_mod, [(503, b'{"error": "model still loading"}', _FAST)])
        got = await _speak(_tts())
        assert len(got) == 1 and isinstance(got[0], SpeechError) and got[0].code == WARMING_UP_CODE
        assert len(wire.requests) == 3

    async def test_a_rejection_is_one_attempt(self, monkeypatch) -> None:
        wire = _route(monkeypatch, tts_mod, [(400, b'{"error": {"message": "bad voice"}}', {})])
        got = await _speak(_tts())
        assert len(got) == 1 and isinstance(got[0], SpeechError) and not got[0].retriable
        assert len(wire.requests) == 1
