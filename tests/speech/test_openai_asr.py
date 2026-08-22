"""OpenAIASR wire shape (S4 P1 Task 6). asr-tts.md section 2 is normative."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from primer.model.provider import (
    Limits,
    SpeechToTextProvider,
    SpeechToTextProviderType,
)
from primer.model.speech import SpeechError, Transcription, WARMING_UP_CODE
from primer.speech.openai_asr import OpenAIASR


def _row(**overrides) -> SpeechToTextProvider:
    body = {
        "id": "stt-a",
        "provider": SpeechToTextProviderType.OPENAI,
        "default_model": "whisper-1",
        "config": {"url": "http://asr.local:8006/v1"},
        "limits": Limits(
            max_concurrency=2,
            connect_timeout_seconds=5.0,
            request_timeout_seconds=120.0,
        ),
    }
    body.update(overrides)
    return SpeechToTextProvider(**body)


class _Transcriptions:
    def __init__(self, result=None, raises=None) -> None:
        self.kwargs = None
        self._result = result
        self._raises = raises

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._result


def _stub_client(transcriptions) -> SimpleNamespace:
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))


@pytest.mark.asyncio
async def test_the_file_part_is_a_filename_fileobj_mimetype_triple(monkeypatch) -> None:
    calls = _Transcriptions(result=SimpleNamespace(text="hello there"))
    adapter = OpenAIASR(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(calls))

    out = await adapter.transcribe(
        model="whisper-1",
        audio=b"RIFFdata",
        filename="clip.wav",
        mimetype="audio/wav",
    )

    assert isinstance(out, Transcription)
    assert out.text == "hello there"
    sent = calls.kwargs["file"]
    assert isinstance(sent, tuple), "a bare file handle sends no filename"
    assert len(sent) == 3
    assert sent[0] == "clip.wav"
    assert sent[1] == b"RIFFdata"
    assert sent[2] == "audio/wav"


@pytest.mark.asyncio
async def test_the_timeout_is_a_connect_read_pair_not_a_scalar(monkeypatch) -> None:
    calls = _Transcriptions(result=SimpleNamespace(text=""))
    adapter = OpenAIASR(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(calls))

    await adapter.transcribe(
        model="whisper-1", audio=b"x", filename="a.wav", mimetype="audio/wav",
    )

    timeout = calls.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 120.0


@pytest.mark.asyncio
async def test_response_format_json_and_optional_language(monkeypatch) -> None:
    calls = _Transcriptions(result=SimpleNamespace(text="bonjour"))
    adapter = OpenAIASR(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(calls))

    await adapter.transcribe(
        model="whisper-1",
        audio=b"x",
        filename="a.wav",
        mimetype="audio/wav",
        language="fr",
    )
    assert calls.kwargs["response_format"] == "json"
    assert calls.kwargs["language"] == "fr"


@pytest.mark.asyncio
async def test_language_is_omitted_when_not_supplied(monkeypatch) -> None:
    calls = _Transcriptions(result=SimpleNamespace(text=""))
    adapter = OpenAIASR(_row())
    monkeypatch.setattr(adapter, "_get_client", lambda: _stub_client(calls))

    await adapter.transcribe(
        model="whisper-1", audio=b"x", filename="a.wav", mimetype="audio/wav",
    )
    assert "language" not in calls.kwargs


@pytest.mark.asyncio
async def test_a_503_becomes_a_retriable_warming_up_value(monkeypatch) -> None:
    class _Boom(Exception):
        status_code = 503

    adapter = OpenAIASR(_row())
    monkeypatch.setattr(
        adapter, "_get_client", lambda: _stub_client(_Transcriptions(raises=_Boom())),
    )

    out = await adapter.transcribe(
        model="whisper-1", audio=b"x", filename="a.wav", mimetype="audio/wav",
    )
    assert isinstance(out, SpeechError)
    assert out.code == WARMING_UP_CODE
    assert out.retriable is True


@pytest.mark.asyncio
async def test_transcribe_never_raises_on_provider_failure(monkeypatch) -> None:
    adapter = OpenAIASR(_row())
    monkeypatch.setattr(
        adapter,
        "_get_client",
        lambda: _stub_client(_Transcriptions(raises=RuntimeError("dropped"))),
    )
    out = await adapter.transcribe(
        model="whisper-1", audio=b"x", filename="a.wav", mimetype="audio/wav",
    )
    assert isinstance(out, SpeechError)


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    closed = {"n": 0}

    class _Client:
        async def close(self):
            closed["n"] += 1

    adapter = OpenAIASR(_row())
    adapter._client = _Client()
    await adapter.aclose()
    await adapter.aclose()
    assert closed["n"] == 1


def test_a_mismatched_provider_type_is_a_config_error() -> None:
    from primer.model.except_ import ConfigError

    row = _row()
    object.__setattr__(row, "provider", "bogus")
    with pytest.raises(ConfigError):
        OpenAIASR(row)
