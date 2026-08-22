"""Retry only when nothing was yielded (S4 P1 Task 8). asr-tts.md section 5."""

from __future__ import annotations

import pytest
from openai import APIConnectionError

from primer.model.provider import (
    Limits,
    TextToSpeechProvider,
    TextToSpeechProviderType,
)
from primer.model.speech import SpeechError
from primer.speech.openai_tts import OpenAITTS
from tests.speech.test_openai_tts import _Ctx, _Response, _Speech, _stub_client


def _row(max_retries: int) -> TextToSpeechProvider:
    return TextToSpeechProvider(
        id="tts-a",
        provider=TextToSpeechProviderType.OPENAI,
        default_model="kokoro",
        default_voice="af_heart",
        config={"url": "http://tts.local:8004/v1"},
        limits=Limits(
            max_concurrency=2,
            max_retries=max_retries,
            retry_backoff_seconds=0.0,
            retry_backoff_max_seconds=0.0,
        ),
    )


def _connect_error() -> APIConnectionError:
    import httpx

    return APIConnectionError(request=httpx.Request("POST", "http://tts.local/v1"))


@pytest.mark.asyncio
async def test_a_connect_failure_with_zero_bytes_yielded_is_retried(monkeypatch) -> None:
    speech = _Speech([_Ctx(raises=_connect_error()), _Ctx(_Response([b"a", b"b"]))])
    adapter = OpenAITTS(_row(max_retries=2))
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert got == [b"a", b"b"]
    assert len(speech.kwargs) == 2, "the connect failure was not replayed"


@pytest.mark.asyncio
async def test_a_failure_after_bytes_were_yielded_is_never_retried(monkeypatch) -> None:
    class _HalfResponse(_Response):
        async def iter_bytes(self, chunk_size=None):
            yield b"first"
            raise _connect_error()

    speech = _Speech([_Ctx(_HalfResponse([])), _Ctx(_Response([b"never"]))])
    adapter = OpenAITTS(_row(max_retries=2))
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert got[0] == b"first"
    assert isinstance(got[1], SpeechError)
    assert len(speech.kwargs) == 1, "a half-consumed stream must never be replayed"


@pytest.mark.asyncio
async def test_a_non_connect_failure_is_not_retried(monkeypatch) -> None:
    class _Boom(Exception):
        status_code = 400

    speech = _Speech([_Ctx(raises=_Boom()), _Ctx(_Response([b"never"]))])
    adapter = OpenAITTS(_row(max_retries=2))
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert isinstance(got[0], SpeechError)
    assert len(speech.kwargs) == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_by_max_retries(monkeypatch) -> None:
    speech = _Speech([_Ctx(raises=_connect_error()) for _ in range(5)])
    adapter = OpenAITTS(_row(max_retries=1))
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert isinstance(got[-1], SpeechError)
    assert len(speech.kwargs) == 2, "max_retries=1 means one initial try plus one replay"


@pytest.mark.asyncio
async def test_max_retries_zero_disables_replay(monkeypatch) -> None:
    speech = _Speech([_Ctx(raises=_connect_error()), _Ctx(_Response([b"never"]))])
    adapter = OpenAITTS(_row(max_retries=0))
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(speech))

    got = [item async for item in adapter.stream(model="kokoro", text="x", voice="v")]
    assert isinstance(got[0], SpeechError)
    assert len(speech.kwargs) == 1
