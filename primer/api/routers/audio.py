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


__all__ = ["audio_router"]
