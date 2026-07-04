"""Admin API-key management (Spec: admin-api-key-management).

Admins view + revoke ANY user's API tokens, reached by drilling down from
the admin Users page. Mounted with ``require_admin``; cookie-only (bearer
credentials cannot manage tokens). Summaries only — plaintext/hash never
returned; plaintext exists once, at creation, for the owning user.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from primer.api.deps import get_storage_provider
from primer.api.routers._api_tokens_shared import (
    ApiTokenListResponse,
    get_token_owned_by,
    list_tokens_for_user,
    require_cookie_session,
    revoke_token,
    to_summary,
)
from primer.model.api_token import ApiToken
from primer.model.user import User

logger = logging.getLogger(__name__)

admin_tokens_router = APIRouter(prefix="/admin/users", tags=["admin", "tokens"])


async def _user_or_404(sp, user_id: str) -> User:
    user = await sp.get_storage(User).get(user_id)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "user_not_found", "message": f"user {user_id!r} does not exist"},
        )
    return user


@admin_tokens_router.get(
    "/{user_id}/tokens",
    response_model=ApiTokenListResponse,
    summary="List a user's API tokens (admin; plaintext omitted).",
)
async def admin_list_user_tokens(
    request: Request,
    user_id: str = Path(...),
    sp=Depends(get_storage_provider),
) -> ApiTokenListResponse:
    require_cookie_session(request)
    await _user_or_404(sp, user_id)
    storage = sp.get_storage(ApiToken)
    rows = await list_tokens_for_user(storage, user_id)
    return ApiTokenListResponse(items=[to_summary(r) for r in rows])


@admin_tokens_router.delete(
    "/{user_id}/tokens/{token_id}",
    status_code=204,
    summary="Revoke a user's API token (admin). Idempotent.",
)
async def admin_revoke_user_token(
    request: Request,
    user_id: str = Path(...),
    token_id: str = Path(...),
    sp=Depends(get_storage_provider),
):
    require_cookie_session(request)
    await _user_or_404(sp, user_id)
    storage = sp.get_storage(ApiToken)
    row = await get_token_owned_by(storage, user_id, token_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": f"api token {token_id!r} does not exist"},
        )
    if await revoke_token(storage, row):
        logger.info("admin_tokens.revoke id=%s user=%s name=%s", row.id, user_id, row.name)
    return JSONResponse(status_code=204, content=None)


__all__ = ["admin_tokens_router"]
