"""Singleton GET + PUT for /v1/speech_active_config (S4 P1 Task 10)."""

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


class TestSingletonGet:
    @pytest.mark.asyncio
    async def test_an_unconfigured_install_gets_an_empty_row_not_a_503(self, client) -> None:
        r = await client.get("/v1/speech_active_config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "_active_speech_config"
        assert body["stt_provider_id"] is None
        assert body["tts_provider_id"] is None
        assert body["tts_voice"] is None


class TestSingletonPut:
    @pytest.mark.asyncio
    async def test_put_stores_both_providers_and_the_voice(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        await client.post("/v1/tts_providers", json=_TTS)
        r = await client.put(
            "/v1/speech_active_config",
            json={
                "stt_provider_id": "stt-a",
                "tts_provider_id": "tts-a",
                "tts_voice": "am_adam",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["tts_voice"] == "am_adam"
        again = await client.get("/v1/speech_active_config")
        assert again.json()["stt_provider_id"] == "stt-a"

    @pytest.mark.asyncio
    async def test_an_unknown_stt_id_is_422_with_the_id_named(self, client) -> None:
        r = await client.put(
            "/v1/speech_active_config", json={"stt_provider_id": "nope"},
        )
        assert r.status_code == 422, r.text
        assert "nope" in r.json()["extensions"]["unknown_ids"]

    @pytest.mark.asyncio
    async def test_an_unknown_tts_id_is_422(self, client) -> None:
        r = await client.put(
            "/v1/speech_active_config", json={"tts_provider_id": "nope"},
        )
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_clearing_back_to_all_none_is_allowed(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        await client.put("/v1/speech_active_config", json={"stt_provider_id": "stt-a"})
        r = await client.put("/v1/speech_active_config", json={})
        assert r.status_code == 200, r.text
        assert r.json()["stt_provider_id"] is None

    @pytest.mark.asyncio
    async def test_the_second_put_updates_rather_than_conflicting(self, client) -> None:
        await client.post("/v1/tts_providers", json=_TTS)
        first = await client.put(
            "/v1/speech_active_config", json={"tts_provider_id": "tts-a"},
        )
        second = await client.put(
            "/v1/speech_active_config",
            json={"tts_provider_id": "tts-a", "tts_voice": "af_heart"},
        )
        assert first.status_code == 200
        assert second.status_code == 200, second.text
        assert second.json()["tts_voice"] == "af_heart"
