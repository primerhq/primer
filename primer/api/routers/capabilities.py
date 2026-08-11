"""Capability discovery: which optional extras this deployment has.

Read-only and computed live from importability (primer.common.optional),
so a pip install + restart is immediately reflected. Consumed by the
console (capability-aware states) and primectl.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

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


class CapabilitiesResponse(BaseModel):
    version: str
    extras: dict[str, ExtraStatus]


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities() -> CapabilitiesResponse:
    extras: dict[str, ExtraStatus] = {}
    for extra in sorted(EXTRA_MODULES):
        status = ExtraStatus(installed=has_extra(extra))
        if extra == "channels":
            status.platforms = channel_platforms()
        extras[extra] = status
    return CapabilitiesResponse(version=APP_VERSION, extras=extras)
