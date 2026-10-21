"""Shared MCP toolset OAuth sign-in routes."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse

from primer.api.deps import get_mcp_oauth, get_principal, get_storage_provider, require_admin
from primer.api.errors import _problem_response, common_responses
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.provider import Toolset, ToolsetProviderType
from primer.toolset.oauth.service import (
    OAuthTarget,
    SignInError,
    SignInStart,
    ToolsetAuthStatus,
    ToolsetOAuthService,
)


mcp_oauth_public_router = APIRouter(tags=["toolset OAuth"])
mcp_oauth_router = APIRouter(tags=["toolset OAuth"])

CONNECT_ADMIN_REASON = (
    "Connecting, reconnecting or disconnecting an MCP toolset's OAuth sign-in requires the admin role: every agent that uses the toolset acts as "
    "whoever connected it."
)


def _require_connect_admin(request: Request) -> None:
    try:
        require_admin(request)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden_role", "message": CONNECT_ADMIN_REASON},
        ) from exc


async def _stored_toolset(toolset_id: str, storage_provider) -> Toolset:
    toolset = await storage_provider.get_storage(Toolset).get(toolset_id)
    if toolset is None:
        raise NotFoundError(f"Toolset {toolset_id!r} does not exist")
    return toolset


def _redirect_uri(service: ToolsetOAuthService, request: Request, toolset_id: str) -> str:
    return service.redirect_uri(toolset_id, request_base_url=str(request.base_url))


def console_return_url(toolset_id: str, outcome: str, code: str | None = None) -> str:
    query_id = quote(toolset_id, safe="")
    overlay_id = quote(toolset_id, safe="/")
    query = f"mcp_oauth={outcome}&mcp_oauth_toolset={query_id}"
    if code is not None:
        query += f"&mcp_oauth_error={quote(code, safe='')}"
    return f"/console/?{query}#/?view=platform:toolsets&overlay=toolsets::{overlay_id}"


@mcp_oauth_router.post(
    "/toolsets/{toolset_id}/oauth/start",
    response_model=SignInStart,
    responses=common_responses(400, 403, 404, 500, 502, 504),
)
async def start_toolset_sign_in(
    request: Request,
    toolset_id: str = Path(...),
    principal: str | None = Depends(get_principal),
    storage_provider=Depends(get_storage_provider),
    service: ToolsetOAuthService = Depends(get_mcp_oauth),
) -> SignInStart:
    _require_connect_admin(request)
    if toolset_id in RESERVED_TOOLSET_IDS:
        toolset = Toolset(id=toolset_id, provider=ToolsetProviderType.INTERNAL)
    else:
        toolset = await _stored_toolset(toolset_id, storage_provider)
    target = OAuthTarget.from_toolset(toolset)
    if target is None:
        raise BadRequestError("this toolset has no OAuth settings (its Authentication is None or Header)")
    if target.oauth.grant == "client_credentials":
        status = await service.connect_client_credentials(target, connected_by=principal)
        return SignInStart(authorize_url=None, redirect_uri=None, expires_at=None, status=status)
    redirect_uri = _redirect_uri(service, request, toolset_id)
    return await service.start_sign_in(target, redirect_uri=redirect_uri, started_by=principal)


@mcp_oauth_public_router.get(
    "/toolsets/{toolset_id}/oauth/callback",
    responses=common_responses(400, 500, 502, 504),
)
async def complete_toolset_sign_in(
    request: Request,
    toolset_id: str = Path(...),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    storage_provider=Depends(get_storage_provider),
    service: ToolsetOAuthService = Depends(get_mcp_oauth),
):
    del error_description
    toolset = await storage_provider.get_storage(Toolset).get(toolset_id)
    wants_html = "text/html" in request.headers.get("accept", "")
    try:
        status = await service.complete_sign_in(
            toolset,
            toolset_id=toolset_id,
            state=state or "",
            code=code,
            error=error,
        )
    except SignInError as exc:
        if toolset is None:
            exc = SignInError("invalid_state", toolset_id)
        if wants_html:
            return RedirectResponse(console_return_url(toolset_id, "error", exc.code), status_code=303)
        return _problem_response(
            request=request,
            status=400,
            type_uri="/errors/mcp-oauth-sign-in",
            title="MCP OAuth Sign-in Failed",
            detail=str(exc),
            extensions={"code": exc.code},
        )
    if wants_html:
        return RedirectResponse(console_return_url(toolset_id, "connected"), status_code=303)
    return {"ok": True, "toolset_id": toolset_id, "status": status.model_dump(mode="json")}


@mcp_oauth_router.get(
    "/toolsets/{toolset_id}/oauth/status",
    response_model=ToolsetAuthStatus,
    responses=common_responses(404, 500),
)
async def toolset_sign_in_status(
    request: Request,
    toolset_id: str = Path(...),
    storage_provider=Depends(get_storage_provider),
    service: ToolsetOAuthService = Depends(get_mcp_oauth),
) -> ToolsetAuthStatus:
    if toolset_id in RESERVED_TOOLSET_IDS:
        toolset = Toolset(id=toolset_id, provider=ToolsetProviderType.INTERNAL)
    else:
        toolset = await _stored_toolset(toolset_id, storage_provider)
    return await service.status(
        toolset,
        redirect_uri=_redirect_uri(service, request, toolset_id),
    )


@mcp_oauth_router.post(
    "/toolsets/{toolset_id}/oauth/disconnect",
    response_model=ToolsetAuthStatus,
    responses=common_responses(403, 404, 500, 502, 504),
)
async def disconnect_toolset_sign_in(
    request: Request,
    toolset_id: str = Path(...),
    storage_provider=Depends(get_storage_provider),
    service: ToolsetOAuthService = Depends(get_mcp_oauth),
) -> ToolsetAuthStatus:
    _require_connect_admin(request)
    toolset = await _stored_toolset(toolset_id, storage_provider)
    return await service.disconnect(toolset)


__all__ = ["CONNECT_ADMIN_REASON", "console_return_url", "mcp_oauth_public_router", "mcp_oauth_router"]
