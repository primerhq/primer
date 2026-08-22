"""POST /v1/audio/transcriptions proxy (S4 P1 Task 12)."""

from __future__ import annotations

import pytest

from primer.model.speech import SpeechError, Transcription


_STT = {
    "id": "stt-a",
    "provider": "openai",
    "default_model": "whisper-1",
    "config": {"url": "http://asr.local:8006/v1"},
    "limits": {"max_concurrency": 1},
}


class _StubASR:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def transcribe(self, *, model, audio, filename, mimetype, language=None):
        self.calls.append(
            {
                "model": model,
                "audio": audio,
                "filename": filename,
                "mimetype": mimetype,
                "language": language,
            }
        )
        return self.result

    async def aclose(self) -> None:
        return


async def _configure(client, app, adapter) -> None:
    await client.post("/v1/stt_providers", json=_STT)
    await client.put("/v1/speech_active_config", json={"stt_provider_id": "stt-a"})
    app.state.stt_registry._instances["stt-a"] = adapter


@pytest.mark.asyncio
async def test_a_configured_provider_returns_the_transcript(client, app) -> None:
    stub = _StubASR(Transcription(text="hello world"))
    await _configure(client, app, stub)

    r = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"RIFFdata", "audio/wav")},
        data={"model": "whisper-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello world"}
    assert stub.calls[0]["filename"] == "clip.wav"
    assert stub.calls[0]["mimetype"] == "audio/wav"
    assert stub.calls[0]["audio"] == b"RIFFdata"


@pytest.mark.asyncio
async def test_the_row_default_model_applies_when_none_is_sent(client, app) -> None:
    stub = _StubASR(Transcription(text=""))
    await _configure(client, app, stub)

    r = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert stub.calls[0]["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_the_language_form_field_is_forwarded(client, app) -> None:
    stub = _StubASR(Transcription(text=""))
    await _configure(client, app, stub)

    await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
        data={"language": "fr"},
    )
    assert stub.calls[0]["language"] == "fr"


@pytest.mark.asyncio
async def test_no_configured_provider_is_503(client, app) -> None:
    r = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
    )
    assert r.status_code == 503, r.text
    assert r.json()["extensions"]["error"] == "stt_not_configured"


@pytest.mark.asyncio
async def test_a_warming_up_provider_maps_to_a_retriable_503(client, app) -> None:
    stub = _StubASR(
        SpeechError(code="warming_up", message="loading", retriable=True),
    )
    await _configure(client, app, stub)

    r = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
    )
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["extensions"]["error"] == "warming_up"
    assert body["extensions"]["retriable"] is True


@pytest.mark.asyncio
async def test_a_hard_provider_failure_maps_to_502(client, app) -> None:
    stub = _StubASR(SpeechError(code="provider_error", message="bad request"))
    await _configure(client, app, stub)

    r = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
    )
    assert r.status_code == 502, r.text
    assert r.json()["extensions"]["retriable"] is False


@pytest.mark.asyncio
async def test_the_proxy_requires_authentication(raw_client) -> None:
    r = await raw_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"x", "audio/wav")},
    )
    assert r.status_code in (401, 403), r.text
