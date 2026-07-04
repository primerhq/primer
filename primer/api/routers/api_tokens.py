"""CRUD router for programmatic-access tokens (Spec §7).

Endpoints (all under ``/v1/auth/tokens``):

* ``POST   /``        — mint a token. Body ``{name, scopes?, expires_at?}``.
                        Returns 201 with the plaintext ONCE
                        (:class:`ApiTokenCreatedResponse`).
* ``GET    /``        — list the caller's tokens. Plaintext NEVER
                        included (:class:`ApiTokenSummary`).
* ``DELETE /{id}``    — revoke. Idempotent. Returns 204. 404 if not
                        owned by the caller (existence-leak hardening).
* ``PUT    /{id}``    — rename. Body ``{name}``. Returns the updated
                        summary row.

All endpoints require a cookie session — bearer tokens cannot mint or
manage other tokens. Operators do this from the console. The dep that
runs at ``include_router`` time enforces this for the cookie case via
``require_auth``; we additionally reject the request when
``request.state.api_token`` is set (i.e. the caller authenticated with a
bearer token), returning 403 ``token_minting_forbidden``.

Validation:

* ``name`` non-empty after strip, ≤128 chars. ``(user_id, name)`` is
  unique — 409 ``token_name_conflict`` on duplicate.
* ``scopes`` accepted open-set; unknown values logged as a warning and
  retained on the row (forward-compat for future scope versions).
* ``expires_at`` must be in the future at the moment of the call — 422
  ``token_expires_in_past`` otherwise.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.deps import get_storage_provider
from primer.api.routers._api_tokens_shared import (
    ApiTokenListResponse,
    ApiTokenSummary,
    get_token_owned_by,
    list_tokens_for_user,
    require_cookie_session,
    revoke_token,
    to_summary,
)
from primer.auth.api_tokens import (
    extract_prefix,
    hash_token,
    mint_plaintext,
)
from primer.model.api_token import KNOWN_SCOPES, ApiToken
from primer.model.storage import OffsetPage
from primer.storage.q import Q


logger = logging.getLogger(__name__)


# The auth router is mounted at ``/v1/auth`` (via the version prefix at
# include time on the parent router); the api-tokens router uses the
# same convention so its endpoints land under ``/v1/auth/tokens``.
api_tokens_router = APIRouter(prefix="/auth/tokens", tags=["auth", "tokens"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class ApiTokenCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ApiTokenRenameBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class ApiTokenCreatedResponse(BaseModel):
    """ONLY response shape that includes the plaintext.

    Returned exactly once from POST /v1/auth/tokens. The operator MUST
    capture the plaintext at this point — there is no second chance.
    """

    id: str
    name: str
    prefix: str
    scopes: list[str]
    plaintext: str
    created_at: datetime
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_user(request: Request):
    """Return the authenticated user. ``require_auth`` runs at the
    include-router layer and 401s before we get here; this helper just
    surfaces the typed user object for the handler body."""
    return getattr(request.state, "user", None)


async def _find_by_user_and_name(storage, user_id: str, name: str) -> ApiToken | None:
    """Return the row with ``(user_id, name)`` or ``None`` if absent."""
    predicate = (
        Q(ApiToken)
        .where("user_id", user_id)
        .where("name", name)
        .build()
    )
    page = await storage.find(predicate, OffsetPage(offset=0, length=1))
    items = list(getattr(page, "items", []))
    return items[0] if items else None


# ---------------------------------------------------------------------------
# POST /v1/auth/tokens — create
# ---------------------------------------------------------------------------


@api_tokens_router.post(
    "",
    status_code=201,
    response_model=ApiTokenCreatedResponse,
    summary="Create an API token. Plaintext returned ONCE.",
)
async def create_api_token(
    body: ApiTokenCreateBody,
    request: Request,
    sp=Depends(get_storage_provider),
) -> ApiTokenCreatedResponse:
    require_cookie_session(request)
    user = _current_user(request)
    if user is None:
        # Defensive: require_auth at include-router level should have
        # 401'd already; this guard means the handler is safe even when
        # mounted without that dep.
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    name = body.name.strip()
    if not name:
        return JSONResponse(
            status_code=422,
            content={
                "code": "token_name_empty",
                "message": "name must be non-empty after stripping whitespace",
            },
        )

    now = _utcnow()
    if body.expires_at is not None and body.expires_at <= now:
        return JSONResponse(
            status_code=422,
            content={
                "code": "token_expires_in_past",
                "message": "expires_at must be in the future",
            },
        )

    storage = sp.get_storage(ApiToken)

    existing = await _find_by_user_and_name(storage, user.id, name)
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content={
                "code": "token_name_conflict",
                "message": f"a token named {name!r} already exists",
            },
        )

    # Forward-compat: unknown scopes accepted but logged. We rely on the
    # model's own field validator to lowercase + dedupe, so we feed it
    # the raw list verbatim.
    unknown = [s for s in body.scopes if s.strip().lower() not in KNOWN_SCOPES and s.strip()]
    if unknown:
        logger.warning(
            "api_tokens.create: unknown scopes accepted "
            "(forward-compat) — user=%s token_name=%s scopes=%r",
            user.id, name, unknown,
        )

    plaintext = mint_plaintext()
    token_hash = hash_token(plaintext)
    prefix = extract_prefix(plaintext)

    row = ApiToken(
        id=f"at-{uuid.uuid4().hex[:12]}",
        user_id=user.id,
        name=name,
        token_hash=token_hash,
        prefix=prefix,
        scopes=list(body.scopes),
        created_at=now,
        expires_at=body.expires_at,
    )
    created = await storage.create(row)
    logger.info(
        "api_tokens.create id=%s user=%s name=%s scopes=%r",
        created.id, user.id, created.name, created.scopes,
    )

    return ApiTokenCreatedResponse(
        id=created.id,
        name=created.name,
        prefix=created.prefix,
        scopes=list(created.scopes),
        plaintext=plaintext,
        created_at=created.created_at,
        expires_at=created.expires_at,
    )


# ---------------------------------------------------------------------------
# GET /v1/auth/tokens — list the caller's tokens
# ---------------------------------------------------------------------------


@api_tokens_router.get(
    "",
    response_model=ApiTokenListResponse,
    summary="List the caller's API tokens (plaintext omitted).",
)
async def list_api_tokens(
    request: Request,
    sp=Depends(get_storage_provider),
) -> ApiTokenListResponse:
    require_cookie_session(request)
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    storage = sp.get_storage(ApiToken)
    rows = await list_tokens_for_user(storage, user.id)
    return ApiTokenListResponse(items=[to_summary(r) for r in rows])


# ---------------------------------------------------------------------------
# DELETE /v1/auth/tokens/{id} — revoke (idempotent)
# ---------------------------------------------------------------------------


@api_tokens_router.delete(
    "/{token_id}",
    status_code=204,
    summary="Revoke an API token. Idempotent.",
)
async def revoke_api_token(
    request: Request,
    token_id: str = Path(...),
    sp=Depends(get_storage_provider),
):
    require_cookie_session(request)
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    storage = sp.get_storage(ApiToken)
    row = await get_token_owned_by(storage, user.id, token_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "token_not_found",
                "message": f"api token {token_id!r} does not exist",
            },
        )

    if await revoke_token(storage, row):
        logger.info(
            "api_tokens.revoke id=%s user=%s name=%s",
            row.id, user.id, row.name,
        )
    # Idempotent — already revoked rows still return 204.
    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# PUT /v1/auth/tokens/{id} — rename
# ---------------------------------------------------------------------------


@api_tokens_router.put(
    "/{token_id}",
    response_model=ApiTokenSummary,
    summary="Rename an API token.",
)
async def rename_api_token(
    body: ApiTokenRenameBody,
    request: Request,
    token_id: str = Path(...),
    sp=Depends(get_storage_provider),
):
    require_cookie_session(request)
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail={"error": "auth_required"})

    new_name = body.name.strip()
    if not new_name:
        return JSONResponse(
            status_code=422,
            content={
                "code": "token_name_empty",
                "message": "name must be non-empty after stripping whitespace",
            },
        )

    storage = sp.get_storage(ApiToken)
    row = await get_token_owned_by(storage, user.id, token_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "token_not_found",
                "message": f"api token {token_id!r} does not exist",
            },
        )

    if new_name != row.name:
        clash = await _find_by_user_and_name(storage, user.id, new_name)
        if clash is not None and clash.id != row.id:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "token_name_conflict",
                    "message": f"a token named {new_name!r} already exists",
                },
            )

    updated = row.model_copy(update={"name": new_name})
    saved = await storage.update(updated)
    return to_summary(saved)


__all__ = [
    "api_tokens_router",
    "ApiTokenCreateBody",
    "ApiTokenRenameBody",
    "ApiTokenSummary",
    "ApiTokenCreatedResponse",
    "ApiTokenListResponse",
]
