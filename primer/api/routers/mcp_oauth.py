"""Shared MCP toolset OAuth sign-in routes.

The callback is public (the authorization server sends the browser to it, without a Primer session), so the browser sign-in is bound to the browser
that started it instead (audit G4-01): the start answer sets :data:`SIGN_IN_COOKIE`, an HttpOnly, ``SameSite=Lax`` cookie holding a fresh nonce,
scoped to the callback path and living as long as the pending sign-in (ten minutes), ``Secure`` when the callback is https. The pending sign-in keeps
only the nonce's digest. The callback hands the cookie to the service, which refuses a missing or different one as ``invalid_state`` and leaves the
pending sign-in to its own browser. ``Lax`` is what lets the cookie arrive: the provider's redirect back is a top-level GET navigation (in the console,
the popup the Connect button opened), which carries a ``Lax`` cookie even across sites.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from primer.api.deps import get_mcp_oauth, get_principal, get_storage_provider, require_admin
from primer.api.errors import _problem_response, common_responses
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.provider import Toolset, ToolsetProviderType
from primer.toolset.oauth.service import (
    PENDING_TTL,
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


#: The cookie that binds a browser sign-in to the browser that started it (see the module docstring).
SIGN_IN_COOKIE = "primer_mcp_oauth"


def _cookie_options(request: Request, redirect_uri: str) -> dict:
    """Where the binding cookie goes: the callback's path only, HttpOnly, ``SameSite=Lax``, and ``Secure`` when the callback is https (or the deployment
    says its cookies are secure), so an http deployment still gets it back."""
    config = getattr(request.app.state, "config", None)
    cookie_secure = bool(getattr(getattr(config, "auth", None), "cookie_secure", False))
    return {
        "path": urlsplit(redirect_uri).path,
        "httponly": True,
        "samesite": "lax",
        "secure": urlsplit(redirect_uri).scheme == "https" or cookie_secure,
    }


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
    response: Response,
    toolset_id: str = Path(...),
    principal: str | None = Depends(get_principal),
    storage_provider=Depends(get_storage_provider),
    service: ToolsetOAuthService = Depends(get_mcp_oauth),
) -> SignInStart:
    """Start the toolset's sign-in (admin only). Client credentials connect at once. A browser sign-in answers the authorize URL and sets the
    ``primer_mcp_oauth`` cookie, which binds the sign-in to this browser: the callback refuses one that does not carry it."""
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
    binding = secrets.token_urlsafe(32)
    started = await service.start_sign_in(target, redirect_uri=redirect_uri, started_by=principal, browser_binding=binding)
    response.set_cookie(SIGN_IN_COOKIE, binding, max_age=int(PENDING_TTL.total_seconds()), **_cookie_options(request, redirect_uri))
    return started


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
    """Finish a browser sign-in (public: the authorization server sends the browser here). It must come back to the browser that started it: a
    request without that browser's ``primer_mcp_oauth`` cookie is ``invalid_state``, and the sign-in stays open for its own browser."""
    del error_description
    toolset = await storage_provider.get_storage(Toolset).get(toolset_id)
    wants_html = "text/html" in request.headers.get("accept", "")
    cookie = _cookie_options(request, _redirect_uri(service, request, toolset_id))
    try:
        status = await service.complete_sign_in(
            toolset,
            toolset_id=toolset_id,
            state=state or "",
            code=code,
            error=error,
            browser_binding=request.cookies.get(SIGN_IN_COOKIE),
        )
    except SignInError as exc:
        spent = exc.code != "invalid_state"             # the binding matched and the sign-in was taken: its cookie has done its job
        if toolset is None:
            exc = SignInError("invalid_state", toolset_id)
        answer: Response
        if wants_html:
            answer = RedirectResponse(console_return_url(toolset_id, "error", exc.code), status_code=303)
        else:
            answer = _problem_response(
                request=request,
                status=400,
                type_uri="/errors/mcp-oauth-sign-in",
                title="MCP OAuth Sign-in Failed",
                detail=str(exc),
                extensions={"code": exc.code},
            )
        if spent:
            answer.delete_cookie(SIGN_IN_COOKIE, **cookie)
        return answer
    if wants_html:
        answer = RedirectResponse(console_return_url(toolset_id, "connected"), status_code=303)
    else:
        answer = JSONResponse({"ok": True, "toolset_id": toolset_id, "status": status.model_dump(mode="json")})
    answer.delete_cookie(SIGN_IN_COOKIE, **cookie)
    return answer


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


__all__ = ["CONNECT_ADMIN_REASON", "SIGN_IN_COOKIE", "console_return_url", "mcp_oauth_public_router", "mcp_oauth_router"]
