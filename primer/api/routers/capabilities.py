"""Capability discovery: which optional extras this deployment has.

Read-only and computed live from importability (primer.common.optional),
so a pip install + restart is immediately reflected. Consumed by the
workspace shell (capability-aware states).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from primer.model.provider import SpeechToTextProvider, TextToSpeechProvider
from primer.api.version import APP_VERSION
from primer.common.optional import EXTRA_MODULES, channel_platforms, has_extra

router = APIRouter(tags=["capabilities"])


class ExtraStatus(BaseModel):
    installed: bool = Field(
        ..., description="True when every marker module of the extra imports."
    )
    platforms: dict[str, bool] | None = Field(
        default=None,
        description="Per-platform detail; only set for the 'channels' extra.",
    )


class SpeechCapabilities(BaseModel):
    """Whether this deployment can hear and speak.

    Sourced from PROVIDER STORAGE, not importability: speech is plain
    HTTP over the openai SDK the LLM adapters already pull in, so there
    is no extra to detect. The console gates its mic on
    ``stt_configured`` and its speaker toggle on ``tts_configured``.
    """

    stt_configured: bool = Field(
        ..., description="True when at least one SpeechToTextProvider row exists.",
    )
    tts_configured: bool = Field(
        ..., description="True when at least one TextToSpeechProvider row exists.",
    )


class CapabilitiesResponse(BaseModel):
    version: str
    extras: dict[str, ExtraStatus]
    speech: SpeechCapabilities


async def _any_row(storage) -> bool:
    from primer.model.storage import OffsetPage

    page = await storage.list(OffsetPage(offset=0, length=1))
    return bool(page.items)


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(request: Request) -> CapabilitiesResponse:
    extras: dict[str, ExtraStatus] = {}
    for extra in sorted(EXTRA_MODULES):
        status = ExtraStatus(installed=has_extra(extra))
        if extra == "channels":
            status.platforms = channel_platforms()
        extras[extra] = status
    storage_provider = request.app.state.storage_provider
    speech = SpeechCapabilities(
        stt_configured=await _any_row(
            storage_provider.get_storage(SpeechToTextProvider)
        ),
        tts_configured=await _any_row(
            storage_provider.get_storage(TextToSpeechProvider)
        ),
    )
    return CapabilitiesResponse(
        version=APP_VERSION, extras=extras, speech=speech,
    )
