"""Server-side audio proxy: transcription in, synthesis out.

Browser code never sees provider auth or CORS -- it talks only to these
routes, which resolve the active speech providers server-side and forward
through the adapters.

Two shapes, and the asymmetry matters: transcription is multipart in with
JSON out; synthesis is JSON in with chunked audio out. Anything that
tries to share one helper between them gets one of the two wrong.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from primer.api.errors import common_responses
from primer.api.routers.speech import read_active_speech_config
from primer.model.provider import SpeechToTextProvider
from primer.model.speech import SpeechError
from collections.abc import AsyncIterator
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse
from primer.model.agent import Agent
from primer.model.provider import TextToSpeechProvider
from primer.speech.resolution import resolve_tts_voice


logger = logging.getLogger(__name__)

audio_router = APIRouter(tags=["audio"])


def _speech_error_http(err: SpeechError) -> HTTPException:
    """Retriable provider states become 503; hard failures become 502."""
    return HTTPException(
        status_code=503 if err.retriable else 502,
        detail={
            "error": err.code,
            "message": err.message,
            "retriable": err.retriable,
        },
    )


async def _stt_context(request: Request):
    """Resolve (row, adapter) for the active speech-to-text provider."""
    active = await read_active_speech_config(request)
    provider_id = active.stt_provider_id
    if not provider_id:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "stt_not_configured",
                "message": (
                    "no speech-to-text provider is active; set one in the "
                    "provider catalog"
                ),
            },
        )
    storage = request.app.state.storage_provider.get_storage(SpeechToTextProvider)
    row = await storage.get(provider_id)
    if row is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "stt_not_configured",
                "message": (
                    f"active speech-to-text provider {provider_id!r} no longer "
                    "exists"
                ),
            },
        )
    adapter = await request.app.state.stt_registry.get(provider_id)
    return row, adapter


@audio_router.post(
    "/audio/transcriptions",
    responses=common_responses(502, 503),
    summary=(
        "Transcribe an uploaded audio segment through the active "
        "speech-to-text provider. Multipart in, JSON out."
    ),
)
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(..., description="Audio segment to transcribe."),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
) -> dict[str, Any]:
    row, adapter = await _stt_context(request)
    audio = await file.read()
    result = await adapter.transcribe(
        model=model or row.default_model,
        audio=audio,
        filename=file.filename or "audio.wav",
        mimetype=file.content_type or "audio/wav",
        language=language,
    )
    if isinstance(result, SpeechError):
        raise _speech_error_http(result)
    return {"text": result.text}


# Container to media type. mp3 is the browser default because it decodes
# as a chunked stream in every modern <audio> element.
_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


class _SpeechBody(BaseModel):
    input: str = Field(..., min_length=1, description="Text to speak.")
    agent_id: str | None = Field(
        default=None,
        description="When set, the agent's tts_voice overrides the default.",
    )
    voice: str | None = Field(
        default=None, description="Explicit voice, beating every default.",
    )
    model: str | None = Field(default=None, description="Synthesis model.")
    response_format: str = Field(default="mp3", description="Audio container.")


async def _pump_audio(
    agen: AsyncIterator[bytes | SpeechError], first: bytes,
) -> AsyncIterator[bytes]:
    """Yield provider chunks THROUGH as they arrive.

    ``first`` is the chunk already pulled to decide the HTTP status: the
    endpoint has to know whether the provider failed before it can
    commit to a 200, and once a StreamingResponse has started there is no
    way to change its status.

    Collecting the remaining chunks and yielding the join would
    reintroduce the whole synthesis wait while still looking like
    streaming. ``tests/api/test_audio_speech.py::
    test_the_pump_yields_chunks_as_they_arrive`` fails if anyone does.
    """
    yield first
    async for item in agen:
        if isinstance(item, SpeechError):
            logger.warning(
                "audio.speech: provider error mid-stream: %s: %s",
                item.code,
                item.message,
            )
            return
        yield item


async def _resolve_voice(request: Request, body: _SpeechBody, row) -> str | None:
    if body.voice:
        return body.voice
    agent_voice: str | None = None
    if body.agent_id:
        storage = request.app.state.storage_provider.get_storage(Agent)
        agent = await storage.get(body.agent_id)
        agent_voice = getattr(agent, "tts_voice", None) if agent is not None else None
    active = await read_active_speech_config(request)
    return resolve_tts_voice(
        agent_tts_voice=agent_voice,
        active_voice=active.tts_voice,
        provider_default_voice=row.default_voice,
    )


@audio_router.post(
    "/audio/speech",
    responses=common_responses(502, 503),
    summary=(
        "Synthesise speech through the active text-to-speech provider. "
        "JSON in, chunked audio out as it is synthesised."
    ),
)
async def synthesize_speech(
    request: Request, body: _SpeechBody,
) -> StreamingResponse:
    active = await read_active_speech_config(request)
    provider_id = active.tts_provider_id
    if not provider_id:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "tts_not_configured",
                "message": (
                    "no text-to-speech provider is active; set one in the "
                    "provider catalog"
                ),
            },
        )
    storage = request.app.state.storage_provider.get_storage(TextToSpeechProvider)
    row = await storage.get(provider_id)
    if row is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "tts_not_configured",
                "message": (
                    f"active text-to-speech provider {provider_id!r} no longer "
                    "exists"
                ),
            },
        )
    adapter = await request.app.state.tts_registry.get(provider_id)
    voice = await _resolve_voice(request, body, row)

    agen = adapter.stream(
        model=body.model or row.default_model,
        text=body.input,
        voice=voice or row.default_voice,
        response_format=body.response_format,
    ).__aiter__()

    # Pull exactly ONE item so a pre-stream failure can still choose a
    # status code. This is not buffering: the first chunk leaves as soon
    # as the provider produces it.
    first: bytes | SpeechError | None = None
    async for item in agen:
        first = item
        break
    if isinstance(first, SpeechError):
        raise _speech_error_http(first)
    if first is None:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "empty_synthesis",
                "message": "the provider returned no audio",
                "retriable": False,
            },
        )

    return StreamingResponse(
        _pump_audio(agen, first),
        media_type=_MEDIA_TYPES.get(
            body.response_format, "application/octet-stream",
        ),
        headers={
            # A reverse proxy or ASGI layer that buffers erases streaming
            # entirely; this is the pair that turns it off.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["audio_router"]
