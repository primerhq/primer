"""REST tests for /v1/stt_providers and /v1/tts_providers (S4 P1 Task 10)."""

from __future__ import annotations

import pytest


_STT = {
    "id": "stt-a",
    "provider": "openai",
    "default_model": "whisper-1",
    "config": {"url": "http://asr.local:8006/v1", "api_key": "sk-secret-XXXX"},
    "limits": {"max_concurrency": 2},
}

_TTS = {
    "id": "tts-a",
    "provider": "openai",
    "default_model": "kokoro",
    "default_voice": "af_heart",
    "config": {"url": "http://tts.local:8004/v1"},
    "limits": {"max_concurrency": 2},
}


class TestSttCrud:
    @pytest.mark.asyncio
    async def test_list_empty(self, client) -> None:
        r = await client.get("/v1/stt_providers?limit=10")
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_create_redacts_the_api_key_in_the_response(self, client) -> None:
        r = await client.post("/v1/stt_providers", json=_STT)
        assert r.status_code in (200, 201), r.text
        assert "sk-secret-XXXX" not in r.text

    @pytest.mark.asyncio
    async def test_create_then_get_round_trips(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        r = await client.get("/v1/stt_providers/stt-a")
        assert r.status_code == 200, r.text
        assert r.json()["default_model"] == "whisper-1"

    @pytest.mark.asyncio
    async def test_unknown_provider_type_is_422(self, client) -> None:
        body = dict(_STT, id="stt-bad", provider="deepgram")
        r = await client.post("/v1/stt_providers", json=body)
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_delete_removes_the_row(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        r = await client.delete("/v1/stt_providers/stt-a")
        assert r.status_code in (200, 204), r.text
        assert (await client.get("/v1/stt_providers/stt-a")).status_code == 404


class TestTtsCrud:
    @pytest.mark.asyncio
    async def test_create_then_get_round_trips(self, client) -> None:
        r = await client.post("/v1/tts_providers", json=_TTS)
        assert r.status_code in (200, 201), r.text
        got = await client.get("/v1/tts_providers/tts-a")
        assert got.json()["default_voice"] == "af_heart"


class TestHelperRoutesBeatTheIdPattern:
    @pytest.mark.asyncio
    async def test_stt_types_is_not_captured_as_an_id(self, client) -> None:
        r = await client.get("/v1/stt_providers/_types")
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"openai"}
        assert r.json()["openai"]["config_fields"] == ["url", "api_key"]
        assert r.json()["openai"]["row_fields"] == ["default_model"]

    @pytest.mark.asyncio
    async def test_tts_types_lists_the_voice_field(self, client) -> None:
        r = await client.get("/v1/tts_providers/_types")
        assert r.status_code == 200, r.text
        assert r.json()["openai"]["row_fields"] == ["default_model", "default_voice"]

    @pytest.mark.asyncio
    async def test_stt_test_reports_ok_with_the_model_list(self, client, monkeypatch) -> None:
        async def _fake_list_models(*, url, api_key, timeout=10.0):
            return ["whisper-1"]

        monkeypatch.setattr(
            "primer.api.routers.speech.list_models", _fake_list_models,
        )
        r = await client.post("/v1/stt_providers/_test", json=_STT)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "models": ["whisper-1"]}

    @pytest.mark.asyncio
    async def test_stt_test_reports_the_failure_without_raising(self, client, monkeypatch) -> None:
        async def _boom(*, url, api_key, timeout=10.0):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("primer.api.routers.speech.list_models", _boom)
        r = await client.post("/v1/stt_providers/_test", json=_STT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "connection refused" in body["error"]

    @pytest.mark.asyncio
    async def test_tts_test_returns_the_voice_list(self, client, monkeypatch) -> None:
        async def _fake_list_voices(*, url, api_key, timeout=10.0):
            return ["af_heart", "am_adam"]

        monkeypatch.setattr(
            "primer.api.routers.speech.list_voices", _fake_list_voices,
        )
        r = await client.post("/v1/tts_providers/_test", json=_TTS)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "voices": ["af_heart", "am_adam"]}

    @pytest.mark.asyncio
    async def test_an_invalid_draft_is_reported_not_raised(self, client) -> None:
        r = await client.post(
            "/v1/stt_providers/_test",
            json={"id": "x", "provider": "openai", "config": {}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        assert "invalid draft" in r.json()["error"]


class TestCascadeBlockOnDelete:
    @pytest.mark.asyncio
    async def test_deleting_the_active_stt_provider_is_blocked(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        await client.put(
            "/v1/speech_active_config", json={"stt_provider_id": "stt-a"},
        )
        r = await client.delete("/v1/stt_providers/stt-a")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["extensions"]["error"] == "cascade_blocked"
        assert body["extensions"]["referenced_by"] == "_active_speech_config"

    @pytest.mark.asyncio
    async def test_deleting_the_active_tts_provider_is_blocked(self, client) -> None:
        await client.post("/v1/tts_providers", json=_TTS)
        await client.put(
            "/v1/speech_active_config", json={"tts_provider_id": "tts-a"},
        )
        r = await client.delete("/v1/tts_providers/tts-a")
        assert r.status_code == 409, r.text


class TestRegistryInvalidation:
    @pytest.mark.asyncio
    async def test_updating_a_row_invalidates_its_cached_adapter(self, client, app) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        adapter = await app.state.stt_registry.get("stt-a")
        updated = dict(_STT, default_model="whisper-large")
        r = await client.put("/v1/stt_providers/stt-a", json=updated)
        assert r.status_code == 200, r.text
        assert await app.state.stt_registry.get("stt-a") is not adapter
