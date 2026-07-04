"""Shared helpers for the self-service (``/v1/auth/tokens``) and admin
(``/v1/admin/users/{user_id}/tokens``) API-token routers.

One implementation of the wire shape (plaintext/hash never included), the
cookie-only guard, the per-user list query, the ownership lookup, and the
idempotent soft-revoke — so the two routers cannot drift.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request
from pydantic import BaseModel

from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.storage.q import Q


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiTokenSummary(BaseModel):
    """Wire shape for list/revoke — plaintext + token_hash NEVER included."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiTokenListResponse(BaseModel):
    items: list[ApiTokenSummary]


def to_summary(row: ApiToken) -> ApiTokenSummary:
    return ApiTokenSummary(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def require_cookie_session(request: Request) -> None:
    """Reject when the caller authenticated via a bearer token.

    The cookie path leaves ``request.state.api_token`` as ``None``; the
    bearer path sets it. Token management is operator-only — bearer
    credentials cannot manage tokens.
    """
    api_token = getattr(request.state, "api_token", None)
    if api_token is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "token_minting_forbidden",
                "message": (
                    "api tokens may only be managed by an operator with a "
                    "cookie session; bearer credentials cannot mint or "
                    "manage other tokens"
                ),
            },
        )


async def list_tokens_for_user(storage, user_id: str) -> list[ApiToken]:
    """Return a user's tokens (up to the 200-row storage page cap)."""
    predicate = Q(ApiToken).where("user_id", user_id).build()
    page = await storage.find(predicate, OffsetPage(offset=0, length=200))
    return list(getattr(page, "items", []))


async def get_token_owned_by(storage, user_id: str, token_id: str) -> ApiToken | None:
    """Look up a token by id, returning ``None`` when missing OR owned by a
    different user (cross-user accesses masked as not-found)."""
    row = await storage.get(token_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def revoke_token(storage, row: ApiToken) -> None:
    """Idempotent soft-revoke: stamp ``revoked_at`` if not already set."""
    if row.revoked_at is None:
        await storage.update(row.model_copy(update={"revoked_at": utcnow()}))
