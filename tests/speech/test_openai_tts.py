"""OpenAITTS streams chunks through as they arrive (S4 P1 Task 7)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from primer.model.provider import (
    Limits,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from primer.model.speech import SpeechError, WARMING_UP_CODE
from primer.speech.openai_tts import OpenAITTS


def _row(**overrides) -> TextToSpeechProvider:
    body = {
        "id": "tts-a",
        "provider": TextToSpeechProviderType.OPENAI,
        "default_model": "kokoro",
        "default_voice": "af_heart",
        "config": {"url": "http://tts.local:8004/v1"},
        "limits": Limits(
            max_concurrency=2,
            connect_timeout_seconds=5.0,
            request_timeout_seconds=300.0,
            max_retries=0,
        ),
    }
    body.update(overrides)
    return TextToSpeechProvider(**body)


class _Response:
    def __init__(self, chunks, gap=0.0, produced=None) -> None:
        self._chunks = chunks
        self._gap = gap
        self._produced = produced if produced is not None else []

    async def iter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            if self._gap:
                await asyncio.sleep(self._gap)
            self._produced.append(time.monotonic())
            yield chunk


class _Ctx:
    def __init__(self, response=None, raises=None) -> None:
        self._response = response
        self._raises = raises

    async def __aenter__(self):
        if self._raises is not None:
            raise self._raises
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _Speech:
    def __init__(self, contexts) -> None:
        self.kwargs = []
        self._contexts = list(contexts)

    @property
    def with_streaming_response(self):
        outer = self

        class _Streaming:
            def create(self, **kwargs):
                outer.kwargs.append(kwargs)
                return outer._contexts.pop(0)

        return _Streaming()


def _stub_client(speech) -> SimpleNamespace:
    return SimpleNamespace(audio=SimpleNamespace(speech=speech))


@pytest.mark.asyncio
async def test_chunks_reach_the_caller_before_the_last_one_is_produced(monkeypatch) -> None:
    """The buffering regression: a collect-then-return adapter fails here."""
    produced: list[float] = []
    speech = _Speech([_Ctx(_Response([b"a", b"b", b"c"], gap=0.05, produced=produced))])
    adapter = OpenAITTS(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    consumed: list[float] = []
    got: list[bytes] = []
    async for chunk in adapter.stream(model="kokoro", text="hi", voice="af_heart"):
        consumed.append(time.monotonic())
        got.append(chunk)

    assert got == [b"a", b"b", b"c"]
    assert consumed[0] < produced[-1], "first chunk was withheld until synthesis ended"


@pytest.mark.asyncio
async def test_request_shape_is_the_openai_speech_body(monkeypatch) -> None:
    speech = _Speech([_Ctx(_Response([b"a"]))])
    adapter = OpenAITTS(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    async for _ in adapter.stream(model="kokoro", text="hello", voice="af_heart"):
        pass

    sent = speech.kwargs[0]
    assert sent["model"] == "kokoro"
    assert sent["input"] == "hello"
    assert sent["voice"] == "af_heart"
    assert sent["response_format"] == "mp3"
    assert isinstance(sent["timeout"], httpx.Timeout)
    assert sent["timeout"].connect == 5.0
    assert sent["timeout"].read == 300.0


@pytest.mark.asyncio
async def test_empty_chunks_are_dropped(monkeypatch) -> None:
    speech = _Speech([_Ctx(_Response([b"", b"a", b"", b"b"]))])
    adapter = OpenAITTS(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))
    got = [c async for c in adapter.stream(model="kokoro", text="x", voice="v")]
    assert got == [b"a", b"b"]


@pytest.mark.asyncio
async def test_a_503_before_any_bytes_yields_a_retriable_error(monkeypatch) -> None:
    class _Boom(Exception):
        status_code = 503

    speech = _Speech([_Ctx(raises=_Boom())])
    adapter = OpenAITTS(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert len(got) == 1
    assert isinstance(got[0], SpeechError)
    assert got[0].code == WARMING_UP_CODE
    assert got[0].retriable is True


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    closed = {"n": 0}

    class _Client:
        async def close(self):
            closed["n"] += 1

    adapter = OpenAITTS(_row())
    adapter._client = _Client()
    await adapter.aclose()
    await adapter.aclose()
    assert closed["n"] == 1
