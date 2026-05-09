"""Health-check endpoint.

Returns 200 with a stable payload identifying the API. Used by
load-balancers and monitoring to verify the process is responsive.
Does not check downstream dependencies (storage, vector store) — that
is a future ``/v1/ready`` endpoint.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from matrix.api.version import APP_VERSION


router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok"] = Field(
        default="ok",
        description="Constant ``ok`` when the process is responsive.",
    )
    version: str = Field(
        ...,
        description="API surface version (semver).",
    )


@router.get(
    "/health",
    response_model=HealthStatus,
    summary="Liveness probe",
)
async def health() -> HealthStatus:
    return HealthStatus(version=APP_VERSION)


__all__ = ["HealthStatus", "router"]
