"""Cookie-based auth router.

Endpoints (all under ``/v1/auth``):

* ``GET  /status``    — returns ``{has_user, authenticated, username}``.
                        Used by the UI to decide register vs login vs main
                        app on initial load. Public.
* ``POST /register``  — body ``{username, password}``. Only valid when no
                        user exists yet (single-user v1). 409 if any user
                        already registered; 422 on weak password. Sets
                        session cookie on success.
* ``POST /login``     — body ``{username, password}``. 401 on bad creds.
                        Sets session cookie on success.
* ``POST /logout``    — clears the session cookie. 204. Idempotent.

The router is registered without an auth guard so unauthenticated users
can hit register/login. The middleware (Commit 5) populates
``request.state.user`` if a valid cookie is present; this router uses
that to decide whether ``status.authenticated`` is True.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from primer.api.deps import get_storage_provider
from primer.auth.passwords import hash_password, verify_password
from primer.auth.tokens import sign_session
from primer.model.user import User
from primer.storage._predicate import FieldRef, Op, Predicate, Value
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["auth"])

_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MIN_PASSWORD_LEN = 8


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)  # not enforcing min on login


class AuthStatus(BaseModel):
    has_user: bool = Field(..., description="True if at least one user exists.")
    authenticated: bool = Field(
        ...,
        description="True if the current request carries a valid session cookie.",
    )
    username: str | None = Field(
        default=None,
        description="Logged-in user's username (only set when authenticated).",
    )


class AuthOk(BaseModel):
    username: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_username(raw: str) -> str:
    return raw.strip().lower()


def _validate_username(name: str) -> None:
    if not _USERNAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_username",
                "message": "username must be 1–64 chars of [a-z 0-9 _ . -]",
            },
        )


async def _has_any_user(request: Request) -> bool:
    """True iff at least one user exists. Cheap: page of length 1."""
    storage = get_storage_provider(request).get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=1))
    return len(page.items) > 0


async def _find_user_by_username(
    request: Request, username: str,
) -> User | None:
    storage = get_storage_provider(request).get_storage(User)
    page = await storage.find(
        Predicate(
            left=FieldRef(name="username"),
            op=Op.EQ,
            right=Value(value=username),
        ),
        OffsetPage(offset=0, length=1),
    )
    return page.items[0] if page.items else None


def _set_session_cookie(request: Request, response: Response, user: User) -> None:
    cfg = request.app.state.config.auth
    secret = request.app.state.session_secret
    token = sign_session(
        user_id=user.id, username=user.username, secret=secret,
    )
    response.set_cookie(
        key=cfg.cookie_name,
        value=token,
        max_age=cfg.session_ttl_days * 86400,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite=cfg.cookie_samesite,
        path="/",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@auth_router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    """Probe used by the UI to pick register / login / main flow."""
    user = getattr(request.state, "user", None)
    has_user = await _has_any_user(request)
    return AuthStatus(
        has_user=has_user,
        authenticated=user is not None,
        username=user.username if user is not None else None,
    )


@auth_router.post("/register", response_model=AuthOk)
async def register(
    body: RegisterBody, request: Request, response: Response,
) -> AuthOk:
    """First-boot operator account creation. Returns 409 if a user
    already exists (single-user v1)."""
    username = _normalise_username(body.username)
    _validate_username(username)

    if await _has_any_user(request):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "user_already_exists",
                "message": "registration is locked; an account already exists",
            },
        )

    pw_hash = await hash_password(body.password)
    user = User(
        id=f"user-{uuid.uuid4().hex[:12]}",
        username=username,
        password_hash=pw_hash,
        created_at=datetime.now(timezone.utc),
    )
    storage = get_storage_provider(request).get_storage(User)
    await storage.create(user)
    _set_session_cookie(request, response, user)
    logger.info("auth.register success username=%s", username)
    return AuthOk(username=username)


@auth_router.post("/login", response_model=AuthOk)
async def login(
    body: LoginBody, request: Request, response: Response,
) -> AuthOk:
    """Verify password, set session cookie."""
    username = _normalise_username(body.username)

    user = await _find_user_by_username(request, username)
    if user is None:
        # Hash a throwaway value to keep timing consistent with the
        # happy path (defence against username-enumeration via timing).
        await hash_password(body.password)
        logger.info("auth.login fail (unknown user) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_credentials"},
        )

    if not await verify_password(body.password, user.password_hash):
        logger.info("auth.login fail (bad password) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_credentials"},
        )

    # Stamp last_login_at.
    user.last_login_at = datetime.now(timezone.utc)
    storage = get_storage_provider(request).get_storage(User)
    await storage.update(user)
    _set_session_cookie(request, response, user)
    logger.info("auth.login success username=%s", username)
    return AuthOk(username=username)


@auth_router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    """Clear the session cookie. Idempotent."""
    cfg = request.app.state.config.auth
    response.delete_cookie(key=cfg.cookie_name, path="/")
