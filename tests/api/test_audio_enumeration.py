"""GET /v1/audio/models and /v1/audio/voices passthroughs (S4 P1 Task 14)."""

from __future__ import annotations

import pytest


_STT = {
    "id": "stt-a",
    "provider": "openai",
    "default_model": "whisper-1",
    "config": {"url": "http://asr.local:8006/v1"},
    "limits": {"max_concurrency": 1},
}

_TTS = {
    "id": "tts-a",
    "provider": "openai",
    "default_model": "kokoro",
    "default_voice": "af_heart",
    "config": {"url": "http://tts.local:8004/v1"},
    "limits": {"max_concurrency": 1},
}


@pytest.mark.asyncio
async def test_models_reports_both_families(client, monkeypatch) -> None:
    async def _fake_models(*, url, api_key, timeout=10.0):
        return ["whisper-1"] if "8006" in url else ["kokoro"]

    monkeypatch.setattr("primer.api.routers.audio.list_models", _fake_models)
    await client.post("/v1/stt_providers", json=_STT)
    await client.post("/v1/tts_providers", json=_TTS)
    await client.put(
        "/v1/speech_active_config",
        json={"stt_provider_id": "stt-a", "tts_provider_id": "tts-a"},
    )

    r = await client.get("/v1/audio/models")
    assert r.status_code == 200, r.text
    assert r.json() == {"stt": ["whisper-1"], "tts": ["kokoro"]}


@pytest.mark.asyncio
async def test_models_is_empty_per_family_when_nothing_is_configured(client) -> None:
    r = await client.get("/v1/audio/models")
    assert r.status_code == 200, r.text
    assert r.json() == {"stt": [], "tts": []}


@pytest.mark.asyncio
async def test_a_probe_failure_degrades_to_an_empty_list(client, monkeypatch) -> None:
    async def _boom(*, url, api_key, timeout=10.0):
        raise RuntimeError("unreachable")

    monkeypatch.setattr("primer.api.routers.audio.list_models", _boom)
    await client.post("/v1/stt_providers", json=_STT)
    await client.put("/v1/speech_active_config", json={"stt_provider_id": "stt-a"})

    r = await client.get("/v1/audio/models")
    assert r.status_code == 200, r.text
    assert r.json()["stt"] == []


@pytest.mark.asyncio
async def test_voices_reads_the_active_tts_provider(client, monkeypatch) -> None:
    async def _fake_voices(*, url, api_key, timeout=10.0):
        return ["af_heart", "am_adam"]

    monkeypatch.setattr("primer.api.routers.audio.list_voices", _fake_voices)
    await client.post("/v1/tts_providers", json=_TTS)
    await client.put("/v1/speech_active_config", json={"tts_provider_id": "tts-a"})

    r = await client.get("/v1/audio/voices")
    assert r.status_code == 200, r.text
    assert r.json() == {"voices": ["af_heart", "am_adam"]}


@pytest.mark.asyncio
async def test_voices_accepts_an_explicit_provider_id(client, monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def _fake_voices(*, url, api_key, timeout=10.0):
        seen["url"] = url
        return ["af_heart"]

    monkeypatch.setattr("primer.api.routers.audio.list_voices", _fake_voices)
    await client.post("/v1/tts_providers", json=_TTS)

    r = await client.get("/v1/audio/voices?provider_id=tts-a")
    assert r.status_code == 200, r.text
    assert "8004" in seen["url"]


@pytest.mark.asyncio
async def test_voices_is_empty_when_no_tts_provider_is_active(client) -> None:
    r = await client.get("/v1/audio/voices")
    assert r.status_code == 200, r.text
    assert r.json() == {"voices": []}
