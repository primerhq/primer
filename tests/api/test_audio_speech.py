"""POST /v1/audio/speech proxy + the buffering guard (S4 P1 Task 13)."""

from __future__ import annotations

import asyncio
import time

import pytest

from primer.api.routers.audio import _pump_audio
from primer.model.speech import SpeechError


_TTS = {
    "id": "tts-a",
    "provider": "openai",
    "default_model": "kokoro",
    "default_voice": "af_heart",
    "config": {"url": "http://tts.local:8004/v1"},
    "limits": {"max_concurrency": 1},
}


class _StubTTS:
    def __init__(self, items, gap: float = 0.0) -> None:
        self._items = items
        self._gap = gap
        self.calls: list[dict] = []

    async def stream(self, *, model, text, voice, response_format="mp3"):
        self.calls.append(
            {
                "model": model,
                "text": text,
                "voice": voice,
                "response_format": response_format,
            }
        )
        for item in self._items:
            if self._gap:
                await asyncio.sleep(self._gap)
            yield item

    async def aclose(self) -> None:
        return


async def _configure(client, app, adapter, **active) -> None:
    await client.post("/v1/tts_providers", json=_TTS)
    body = {"tts_provider_id": "tts-a"}
    body.update(active)
    await client.put("/v1/speech_active_config", json=body)
    app.state.tts_registry._instances["tts-a"] = adapter


# ---------- the buffering regression guard ----------------------------
#
# This cannot be asserted over httpx: ASGITransport accumulates every
# http.response.body message into body_parts and only builds the response
# stream after the app returns, so an in-process HTTP client always sees
# the whole body at once. The pump is therefore driven directly.


@pytest.mark.asyncio
async def test_the_pump_yields_chunks_as_they_arrive() -> None:
    produced: list[float] = []

    async def _provider():
        for chunk in (b"b", b"c"):
            await asyncio.sleep(0.05)
            produced.append(time.monotonic())
            yield chunk

    consumed: list[float] = []
    got: list[bytes] = []
    async for chunk in _pump_audio(_provider(), b"a"):
        consumed.append(time.monotonic())
        got.append(chunk)

    assert got == [b"a", b"b", b"c"]
    assert consumed[0] < produced[-1], (
        "first byte was withheld until the last provider chunk arrived; "
        "the pump is collecting instead of yielding through"
    )


@pytest.mark.asyncio
async def test_the_pump_stops_at_a_terminal_error_value() -> None:
    async def _provider():
        yield b"b"
        yield SpeechError(code="provider_error", message="dropped")
        yield b"never"

    got = [chunk async for chunk in _pump_audio(_provider(), b"a")]
    assert got == [b"a", b"b"]


# ---------- HTTP surface ---------------------------------------------


@pytest.mark.asyncio
async def test_the_response_carries_the_anti_buffering_headers(client, app) -> None:
    await _configure(client, app, _StubTTS([b"b", b"c"]))
    r = await client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code == 200, r.text
    assert r.headers["x-accel-buffering"] == "no"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == b"bc"


@pytest.mark.asyncio
async def test_the_row_defaults_apply_when_the_body_omits_them(client, app) -> None:
    stub = _StubTTS([b"b"])
    await _configure(client, app, stub)
    await client.post("/v1/audio/speech", json={"input": "hello"})
    assert stub.calls[0]["model"] == "kokoro"
    assert stub.calls[0]["voice"] == "af_heart"
    assert stub.calls[0]["response_format"] == "mp3"


@pytest.mark.asyncio
async def test_the_active_config_voice_beats_the_row_default(client, app) -> None:
    stub = _StubTTS([b"b"])
    await _configure(client, app, stub, tts_voice="am_adam")
    await client.post("/v1/audio/speech", json={"input": "hello"})
    assert stub.calls[0]["voice"] == "am_adam"


@pytest.mark.asyncio
async def test_the_agent_override_beats_the_active_config_voice(client, app) -> None:
    stub = _StubTTS([b"b"])
    await _configure(client, app, stub, tts_voice="am_adam")
    created = await client.post(
        "/v1/agents",
        json={
            "id": "agent-voice",
            "description": "voiced agent",
            "model": {"profile_id": "prov--m"},
            "tts_voice": "af_bella",
        },
    )
    assert created.status_code in (200, 201), created.text

    await client.post(
        "/v1/audio/speech", json={"input": "hello", "agent_id": "agent-voice"},
    )
    assert stub.calls[0]["voice"] == "af_bella"


@pytest.mark.asyncio
async def test_an_explicit_body_voice_wins_over_everything(client, app) -> None:
    stub = _StubTTS([b"b"])
    await _configure(client, app, stub, tts_voice="am_adam")
    await client.post(
        "/v1/audio/speech", json={"input": "hello", "voice": "af_nicole"},
    )
    assert stub.calls[0]["voice"] == "af_nicole"


@pytest.mark.asyncio
async def test_no_configured_provider_is_503(client, app) -> None:
    r = await client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code == 503, r.text
    assert r.json()["extensions"]["error"] == "tts_not_configured"


@pytest.mark.asyncio
async def test_a_warming_up_provider_maps_to_a_retriable_503(client, app) -> None:
    stub = _StubTTS(
        [SpeechError(code="warming_up", message="loading", retriable=True)],
    )
    await _configure(client, app, stub)
    r = await client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code == 503, r.text
    assert r.json()["extensions"]["error"] == "warming_up"
    assert r.json()["extensions"]["retriable"] is True


@pytest.mark.asyncio
async def test_a_hard_failure_before_any_audio_maps_to_502(client, app) -> None:
    stub = _StubTTS([SpeechError(code="provider_error", message="nope")])
    await _configure(client, app, stub)
    r = await client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code == 502, r.text


@pytest.mark.asyncio
async def test_pcm_selects_its_own_media_type(client, app) -> None:
    await _configure(client, app, _StubTTS([b"b"]))
    r = await client.post(
        "/v1/audio/speech", json={"input": "hello", "response_format": "pcm"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/pcm")


@pytest.mark.asyncio
async def test_the_proxy_requires_authentication(raw_client) -> None:
    r = await raw_client.post("/v1/audio/speech", json={"input": "hello"})
    assert r.status_code in (401, 403), r.text
