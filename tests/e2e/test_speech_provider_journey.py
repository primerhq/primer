"""Speech round trip against a live server with a stub provider (S4 P1).

The stub speaks the OpenAI audio shape on localhost, so the journey
exercises the real routers, registries and adapters end to end without
depending on a GPU box being reachable from CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

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
async def test_speech_is_consumed_incrementally(client, api_prefix, stub_provider) -> None:
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

    first_byte_at = None
    body = b""
    started = time.monotonic()
    async with client.stream(
        "POST", f"{api_prefix}/audio/speech", json={"input": "hello"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-accel-buffering"] == "no"
        async for chunk in response.aiter_bytes():
            if chunk and first_byte_at is None:
                first_byte_at = time.monotonic()
            body += chunk
    finished_at = time.monotonic()

    assert body.startswith(b"ID3")
    assert len(body) == 3 * (4 + _CHUNK_BYTES), len(body)
    # The stub sleeps 50 ms per chunk for three chunks. A proxy that
    # buffered the whole synthesis would hand over every byte at once,
    # so the gap between the first byte and the last would collapse.
    #
    # Measured as a GAP rather than as an absolute deadline. The absolute
    # form raced the runner: a 140 ms budget against a 150 ms synthesis
    # fails whenever scheduling costs 40 ms, and it did. The gap is the
    # property actually under test and does not depend on how long the
    # first byte took to arrive.
    assert first_byte_at is not None
    assert finished_at - first_byte_at > 0.05, (
        f"stream delivered in one shot: first byte at "
        f"{first_byte_at - started:.3f}s, last at {finished_at - started:.3f}s"
    )


@pytest.mark.asyncio
async def test_capabilities_reports_both_families(client, api_prefix, stub_provider) -> None:
    r = await client.get(f"{api_prefix}/capabilities")
    assert r.status_code == 200, r.text
    assert set(r.json()["speech"]) == {"stt_configured", "tts_configured"}
