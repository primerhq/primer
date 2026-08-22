"""Speech round trip against a live server with a stub provider (S4 P1).

The stub speaks the OpenAI audio shape on localhost, so the journey
exercises the real routers, registries and adapters end to end without
depending on a GPU box being reachable from CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, StreamingResponse


# Per-chunk payload for the streaming stub; see _chunks below.
_CHUNK_BYTES = 64 * 1024


def _stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> dict:
        return {"data": [{"id": "stub-asr"}, {"id": "stub-tts"}]}

    @app.get("/v1/audio/voices")
    async def voices() -> dict:
        return {"voices": ["stub_voice"]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> JSONResponse:
        form = await request.form()
        upload = form["file"]
        return JSONResponse({"text": f"heard {upload.filename}"})

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> StreamingResponse:
        body = json.loads(await request.body())
        assert body["response_format"] == "mp3"

        async def _chunks():
            # Big enough that each chunk is its own delivery. At four
            # bytes a chunk the whole 12-byte body coalesces into one
            # TCP segment, so a streaming proxy and a buffering one look
            # identical from the client and the test could not tell them
            # apart whichever way it asserted.
            for index in range(3):
                await asyncio.sleep(0.05)
                yield b"ID3" + bytes([index]) + b"\0" * _CHUNK_BYTES

        return StreamingResponse(_chunks(), media_type="audio/mpeg")

    return app


@pytest.fixture
async def stub_provider():
    config = uvicorn.Config(_stub_app(), host="127.0.0.1", port=8791, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield "http://127.0.0.1:8791/v1"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_transcription_round_trip(client, api_prefix, stub_provider) -> None:
    await client.post(
        f"{api_prefix}/stt_providers",
        json={
            "id": "e2e-stt",
            "provider": "openai",
            "default_model": "stub-asr",
            "config": {"url": stub_provider},
            "limits": {"max_concurrency": 1},
        },
    )
    await client.put(
        f"{api_prefix}/speech_active_config", json={"stt_provider_id": "e2e-stt"},
    )

    r = await client.post(
        f"{api_prefix}/audio/transcriptions",
        files={"file": ("clip.wav", b"RIFFdata", "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "heard clip.wav"


@pytest.mark.asyncio
async def test_speech_round_trips_and_declares_no_buffering(
    client, api_prefix, stub_provider
) -> None:
    """The synthesis round trip, and the header that keeps it unbuffered.

    This test used to time the delivery and assert the first byte beat
    the full synthesis. It never could, in either form tried: the
    original 140 ms budget raced a 150 ms synthesis, and measuring the
    first-to-last gap instead showed the whole body arriving in a single
    read even at 64 KiB a chunk. What it was really measuring is whether
    the transport in front of the server coalesces, not whether the
    application streams.

    The application property is pinned where it is deterministic, in
    ``tests/api/test_audio_speech.py::
    test_the_pump_yields_chunks_as_they_arrive``, which fails outright
    if the endpoint ever collects the chunks and yields the join. Left
    here: that the round trip works end to end, that the bytes are
    whole, and that the response still tells intermediaries not to
    buffer it.
    """
    await client.post(
        f"{api_prefix}/tts_providers",
        json={
            "id": "e2e-tts",
            "provider": "openai",
            "default_model": "stub-tts",
            "default_voice": "stub_voice",
            "config": {"url": stub_provider},
            "limits": {"max_concurrency": 1},
        },
    )
    await client.put(
        f"{api_prefix}/speech_active_config", json={"tts_provider_id": "e2e-tts"},
    )

    body = b""
    async with client.stream(
        "POST", f"{api_prefix}/audio/speech", json={"input": "hello"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-accel-buffering"] == "no"
        async for chunk in response.aiter_bytes():
            body += chunk

    assert body.startswith(b"ID3")
    # Every chunk the stub produced, in order and unmangled.
    assert len(body) == 3 * (4 + _CHUNK_BYTES), len(body)
    for index in range(3):
        at = index * (4 + _CHUNK_BYTES)
        assert body[at:at + 4] == b"ID3" + bytes([index]), index


@pytest.mark.asyncio
async def test_capabilities_reports_both_families(client, api_prefix, stub_provider) -> None:
    r = await client.get(f"{api_prefix}/capabilities")
    assert r.status_code == 200, r.text
    assert set(r.json()["speech"]) == {"stt_configured", "tts_configured"}
