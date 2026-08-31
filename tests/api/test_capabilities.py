"""GET /v1/capabilities reports installed optional extras."""

from __future__ import annotations

import pytest

import primer.common.optional as optional_mod
from primer.api.version import APP_VERSION
from primer.common.optional import EXTRA_MODULES


@pytest.mark.asyncio
async def test_capabilities_shape(client) -> None:
    response = await client.get("/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == APP_VERSION
    assert set(body["extras"]) == set(EXTRA_MODULES)
    for extra, status in body["extras"].items():
        assert isinstance(status["installed"], bool)
        if extra == "channels":
            assert set(status["platforms"]) == {"slack", "telegram", "discord"}
        else:
            assert status["platforms"] is None


@pytest.mark.asyncio
async def test_capabilities_reflects_missing_extras(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    response = await client.get("/v1/capabilities")
    body = response.json()
    assert all(not s["installed"] for s in body["extras"].values())
    assert body["extras"]["channels"]["platforms"] == {
        "slack": False,
        "telegram": False,
        "discord": False,
    }
class TestSpeechPresence:
    """M11g: speech presence is a PROVIDER FACT, not an extra.

    Speech is plain HTTP, so there is no extra to install; the honest
    signal is whether a provider row exists, which is what the console's
    mic and speaker gating reads.
    """

    @pytest.mark.asyncio
    async def test_speech_is_absent_on_a_fresh_install(self, client) -> None:
        r = await client.get("/v1/capabilities")
        assert r.status_code == 200, r.text
        speech = r.json()["speech"]
        assert speech == {"stt_configured": False, "tts_configured": False}

    @pytest.mark.asyncio
    async def test_registering_an_stt_provider_flips_stt_configured(self, client) -> None:
        await client.post(
            "/v1/stt_providers",
            json={
                "id": "stt-a",
                "provider": "openai",
                "default_model": "whisper-1",
                "config": {"url": "http://asr.local:8006/v1"},
                "limits": {"max_concurrency": 1},
            },
        )
        r = await client.get("/v1/capabilities")
        assert r.json()["speech"]["stt_configured"] is True
        assert r.json()["speech"]["tts_configured"] is False

    @pytest.mark.asyncio
    async def test_registering_a_tts_provider_flips_tts_configured(self, client) -> None:
        await client.post(
            "/v1/tts_providers",
            json={
                "id": "tts-a",
                "provider": "openai",
                "default_model": "kokoro",
                "default_voice": "af_heart",
                "config": {"url": "http://tts.local:8004/v1"},
                "limits": {"max_concurrency": 1},
            },
        )
        r = await client.get("/v1/capabilities")
        assert r.json()["speech"]["tts_configured"] is True

    @pytest.mark.asyncio
    async def test_speech_is_not_reported_as_an_extra(self, client) -> None:
        r = await client.get("/v1/capabilities")
        assert "speech" not in r.json()["extras"]


@pytest.mark.asyncio
async def test_capabilities_has_no_docling_row(client) -> None:
    """S2 removed the docling extra; the endpoint must not advertise it."""
    body = (await client.get("/v1/capabilities")).json()
    assert "docling" not in body["extras"]


@pytest.mark.asyncio
async def test_capabilities_carries_speech_presence(client) -> None:
    """Crosscheck M11(g): speech presence is explicit, not extra-derived.

    Speech adds no extra (S4), so a caller cannot infer it from `extras`;
    the endpoint reports configured-ness from provider storage instead.
    """
    body = (await client.get("/v1/capabilities")).json()
    speech = body["speech"]
    assert set(speech) == {"stt_configured", "tts_configured"}
    assert isinstance(speech["stt_configured"], bool)
    assert isinstance(speech["tts_configured"], bool)
