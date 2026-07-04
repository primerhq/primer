"""REST router for MCP exposure management — Spec §10.

Endpoints (all under ``/v1/mcp_exposure``):

* ``GET    /``           — return the singleton :class:`McpExposure`
                           row. Creates the row lazily on first call so
                           a fresh install always has something to read.
* ``PUT    /``           — Body ``{enabled?, allowed_tools?}``. Mutates
                           the singleton in place; ``None`` fields are
                           left untouched. Allowed-tools are validated
                           against the live catalogue before persisting.
* ``GET    /available``  — enrich every catalogue tool with its
                           exposability verdict and live allowlist
                           membership; powers the Phase 8 UI table.

Auth surface mirrors :mod:`primer.api.routers.api_tokens` — the cookie
session is the only legal write path. Bearer tokens can READ both
endpoints (so an operator-side dashboard authenticated with a token
can still surface the current state) but mutating the exposure config
requires a real operator session. The cookie gate is enforced by the
``_require_cookie_session`` helper, identical in shape to the token
router so the rejection codes stay symmetric.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from primer.api.deps import (
    get_provider_registry,
    get_storage_provider,
    require_admin,
)
from primer.mcp.exposure import (
    ExposureDeps,
    ToolNotExposable,
    ToolUnknown,
    get_exposure,
    list_available_tools,
    update_exposure,
)


logger = logging.getLogger(__name__)


mcp_exposure_router = APIRouter(prefix="/mcp_exposure", tags=["mcp"])


def _require_cookie_session(request: Request) -> None:
    """Reject mutating callers that authenticated with a bearer token.

    The cookie path leaves ``request.state.api_token`` as ``None``;
    bearer auth sets it. MCP exposure config is an operator-only
    surface — bearer credentials must not be able to widen or narrow
    the allowlist, even when they otherwise carry the ``mcp`` scope.
    """
    api_token = getattr(request.state, "api_token", None)
    if api_token is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "mcp_exposure_cookie_only",
                "message": (
                    "MCP exposure config can only be edited via the "
                    "console (cookie session), not bearer tokens."
                ),
            },
        )


class UpdateExposureBody(BaseModel):
    """PATCH-shaped body — ``None`` fields are left unchanged."""

    enabled: bool | None = None
    allowed_tools: list[str] | None = None


@mcp_exposure_router.get("")
async def get_exposure_endpoint(
    request: Request,
    storage_provider=Depends(get_storage_provider),
    provider_registry=Depends(get_provider_registry),
):
    """Return the singleton row, lazily creating it on first call."""
    del request  # unused; auth enforced at include-router level
    deps = ExposureDeps(
        storage_provider=storage_provider,
        provider_registry=provider_registry,
    )
    row = await get_exposure(deps)
    return row.model_dump(mode="json")


@mcp_exposure_router.put("", dependencies=[Depends(require_admin)])
async def update_exposure_endpoint(
    request: Request,
    body: UpdateExposureBody,
    storage_provider=Depends(get_storage_provider),
    provider_registry=Depends(get_provider_registry),
):
    """Mutate the singleton row.

    Unknown scoped ids raise 422 ``tool_unknown``; ids blocked by the
    safety floor raise 422 ``tool_not_exposable`` carrying the denial
    reason so the UI can render a meaningful error toast.
    """
    _require_cookie_session(request)
    deps = ExposureDeps(
        storage_provider=storage_provider,
        provider_registry=provider_registry,
    )
    principal = getattr(request.state, "principal", None)
    try:
        row = await update_exposure(
            enabled=body.enabled,
            allowed_tools=body.allowed_tools,
            updated_by=principal,
            deps=deps,
        )
    except ToolUnknown as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "tool_unknown",
                "scoped_id": exc.scoped_id,
                "message": str(exc),
            },
        )
    except ToolNotExposable as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "tool_not_exposable",
                "scoped_id": exc.scoped_id,
                "reason": exc.reason,
                "message": str(exc),
            },
        )
    return row.model_dump(mode="json")


@mcp_exposure_router.get("/available")
async def available_tools_endpoint(
    request: Request,
    storage_provider=Depends(get_storage_provider),
    provider_registry=Depends(get_provider_registry),
):
    """Enumerate every catalogue tool with exposability + allowlist flags.

    Powers the operator console table — each row carries the
    scoped id, owning toolset, free-form description, the
    :func:`is_exposable` verdict (with denial reason when blocked),
    and whether the tool is in the live allowlist. Broken toolset
    providers are logged + skipped by the service layer so a single
    failing enumerator never blanks the picker.
    """
    del request  # unused; auth enforced at include-router level
    deps = ExposureDeps(
        storage_provider=storage_provider,
        provider_registry=provider_registry,
    )
    items = await list_available_tools(deps)
    return {"items": items}


__all__ = ["mcp_exposure_router"]
