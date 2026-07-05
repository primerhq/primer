"""OIDC SSO login + callback routes — the core Layer 2 auth flow.

Public router (no auth dependency — these endpoints ARE the login
surface), mounted alongside :data:`primer.api.routers.auth.auth_router`.

Endpoints (all under ``/v1/auth/sso``):

* ``GET /providers``               — enabled :class:`OidcProvider` rows
  as ``[{id, name}]``. Used by the login page to render SSO buttons.
* ``GET /{provider_id}/login``     — starts the Authorization Code +
  PKCE flow: discovers the provider, mints a fresh PKCE pair / state /
  nonce, stashes them (plus the resolved ``return_to``) in a signed,
  short-lived, HttpOnly cookie, and 302s the browser to the provider's
  ``authorization_endpoint``.
* ``GET /{provider_id}/callback`` — completes the flow: verifies the
  signed state cookie, exchanges the code, validates the id_token, and
  resolves a local account.

Security boundary — account resolution
---------------------------------------
The callback resolves a local :class:`~primer.model.user.User` from the
verified id_token claims **strictly** by the ``(provider_id, sub)`` pair
recorded in a :class:`~primer.model.oidc.UserIdentity` row:

* An existing ``UserIdentity`` for ``(provider_id, sub)`` maps to its
  ``User`` — a ``disabled`` user is rejected outright.
* Absent that, JIT-provisioning (a brand-new local account) only
  happens when ``system_state.sso_jit_enabled`` is set; otherwise the
  login is rejected.
* **Email is never used to match or link accounts.** It is stored on
  the ``User``/``UserIdentity`` rows purely for display, and only when
  the id_token's ``email_verified`` claim is exactly ``True`` — an
  unverified email from the provider must never be trusted as a
  linking key or even trusted for display.

JIT-creating a ``User`` + its ``UserIdentity`` is two writes; a
concurrent second callback for the same brand-new ``(provider_id,
sub)`` could race between them. The ``UserIdentity`` write is protected
by a DB-level unique constraint on ``(provider_id, subject)`` (see
``tests/storage/test_useridentity_unique.py``), so on that race the
loser catches :class:`ConflictError`, discards the orphaned ``User`` it
just created, and re-resolves to the winner's row instead of erroring.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from primer.api.deps import (
    get_oidc_provider_storage,
    get_storage_provider,
    get_user_identity_storage,
    get_user_storage,
)
from primer.api.routers.auth import _set_session_cookie
from primer.auth import oidc
from primer.model.except_ import ConflictError
from primer.model.oidc import OidcProvider, UserIdentity
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.q import Q


logger = logging.getLogger(__name__)

sso_router = APIRouter(prefix="/auth/sso", tags=["auth", "sso"])

# Cookie carrying the signed {provider_id, nonce, code_verifier,
# return_to} payload between /login and /callback. Short-lived: an
# abandoned login attempt's cookie is worthless after this window.
_STATE_COOKIE_NAME = "primer_sso_state"
_STATE_MAX_AGE_SECONDS = 600

_DEFAULT_RETURN_TO = "/console/"

# Mirrors auth.py / admin_users.py's username shape.
_USERNAME_SANITIZE_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_USERNAME_LEN = 64

# Storage layer's max OffsetPage length (see admin_users.py) — loop
# across pages so provider-listing stays correct past it.
_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SsoProviderOut(BaseModel):
    id: str
    name: str


# ---------------------------------------------------------------------------
# Helpers — open-redirect guard
# ---------------------------------------------------------------------------


def _safe_return_to(value: str | None) -> str:
    """Validate *value* is a same-origin, in-app path; else the console.

    Guards against open-redirect: rejects absolute URLs (a scheme or
    netloc), protocol-relative URLs (``//evil.com``), and the
    backslash variant some browsers normalise to ``//`` (``/\\evil.com``).
    """
    if not value or not value.startswith("/"):
        return _DEFAULT_RETURN_TO
    if value.startswith("//") or value.startswith("/\\"):
        return _DEFAULT_RETURN_TO
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return _DEFAULT_RETURN_TO
    return value


# ---------------------------------------------------------------------------
# Helpers — account resolution (the security boundary)
# ---------------------------------------------------------------------------


async def _find_identity(
    identity_storage, provider_id: str, subject: str,
) -> UserIdentity | None:
    predicate = (
        Q(UserIdentity).where("provider_id", provider_id).where("subject", subject).build()
    )
    page = await identity_storage.find(predicate, OffsetPage(offset=0, length=1))
    return page.items[0] if page.items else None


async def _find_user_by_username(user_storage, username: str) -> User | None:
    predicate = Q(User).where("username", username).build()
    page = await user_storage.find(predicate, OffsetPage(offset=0, length=1))
    return page.items[0] if page.items else None


async def _derive_unique_username(
    user_storage, *, provider_id: str, subject: str, email: str | None,
) -> str:
    """Pick a username that doesn't collide, preferring the email's local part.

    Application-level uniqueness check (mirrors auth.py's register /
    admin_users.py's create — ``User.username`` has no DB-level unique
    constraint). Not airtight against a concurrent race on its own; the
    (provider_id, sub) race is what actually matters and IS DB-enforced
    on ``UserIdentity`` (see :func:`_resolve_or_provision_user`).
    """
    if email and "@" in email:
        base = email.split("@", 1)[0]
    else:
        base = f"{provider_id}-{subject}"
    base = _USERNAME_SANITIZE_RE.sub("-", base.lower()).strip("-._") or "sso-user"
    base = base[: _MAX_USERNAME_LEN - 8]

    candidate = base
    suffix = 1
    while await _find_user_by_username(user_storage, candidate) is not None:
        suffix += 1
        if suffix > 50:
            candidate = f"{base}-{uuid.uuid4().hex[:8]}"
            break
        candidate = f"{base}-{suffix}"
    return candidate


def _reject(status_code: int, error: str, message: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"error": error}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status_code, detail=detail)


async def _resolve_or_provision_user(
    *,
    storage_provider,
    user_storage,
    identity_storage,
    provider_id: str,
    subject: str,
    email: str | None,
) -> User:
    """Resolve strictly on ``(provider_id, subject)``. Never auto-links by email."""
    identity = await _find_identity(identity_storage, provider_id, subject)
    if identity is not None:
        user = await user_storage.get(identity.user_id)
        if user is None:
            # Data-integrity gap (identity row survived a user delete) —
            # treat as unresolvable, never silently re-provision.
            raise _reject(403, "account_disabled", "identity has no matching account")
        if user.disabled:
            raise _reject(403, "account_disabled")
        return user

    state = await storage_provider.get_system_state()
    if not state.sso_jit_enabled:
        raise _reject(403, "sso_jit_disabled", "no linked account and JIT provisioning is off")

    role = state.sso_default_access or "restricted"
    username = await _derive_unique_username(
        user_storage, provider_id=provider_id, subject=subject, email=email,
    )
    now = datetime.now(timezone.utc)
    new_user = User(
        id=f"user-{uuid.uuid4().hex[:12]}",
        username=username,
        email=email,
        password_hash=None,
        role=role,
        created_at=now,
    )
    created_user = await user_storage.create(new_user)
    try:
        await identity_storage.create(
            UserIdentity(
                user_id=created_user.id,
                provider_id=provider_id,
                subject=subject,
                email=email,
                created_at=now,
            )
        )
    except ConflictError:
        # Lost the (provider_id, subject) race to a concurrent callback.
        # Discard our orphaned User and re-resolve to the winner's.
        with contextlib.suppress(Exception):
            await user_storage.delete(created_user.id)
        winner_identity = await _find_identity(identity_storage, provider_id, subject)
        if winner_identity is None:
            # Should not happen (the conflict proves a row exists) — fail
            # closed rather than provisioning a second account.
            raise _reject(409, "sso_race_unresolved") from None
        winner = await user_storage.get(winner_identity.user_id)
        if winner is None:
            raise _reject(403, "account_disabled", "identity has no matching account") from None
        if winner.disabled:
            raise _reject(403, "account_disabled") from None
        return winner
    logger.info(
        "sso.jit_create provider_id=%s user_id=%s username=%s role=%s",
        provider_id, created_user.id, created_user.username, created_user.role,
    )
    return created_user


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@sso_router.get("/providers", response_model=list[SsoProviderOut])
async def list_sso_providers(
    storage=Depends(get_oidc_provider_storage),
) -> list[SsoProviderOut]:
    rows: list[OidcProvider] = []
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE_SIZE))
        rows.extend(page.items)
        if len(page.items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return [SsoProviderOut(id=p.id, name=p.name) for p in rows if p.enabled]


@sso_router.get("/{provider_id}/login")
async def sso_login(
    provider_id: str,
    request: Request,
    return_to: str | None = Query(default=None),
    storage=Depends(get_oidc_provider_storage),
) -> RedirectResponse:
    provider = await storage.get(provider_id)
    if provider is None or not provider.enabled:
        raise _reject(404, "provider_not_found")

    try:
        metadata = await oidc.discover(provider.discovery_url)
        await oidc.fetch_jwks(metadata.jwks_uri)
    except oidc.OidcError as exc:
        raise _reject(502, "provider_unreachable", str(exc)) from exc

    code_verifier, code_challenge = oidc.make_pkce()
    state_value = oidc.gen_state()
    nonce = oidc.gen_nonce()
    safe_return_to = _safe_return_to(return_to)

    redirect_uri = str(request.url_for("sso_callback", provider_id=provider.id))

    secret = request.app.state.session_secret
    cookie_value = oidc.sign_state(
        {
            "provider_id": provider.id,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "return_to": safe_return_to,
        },
        secret,
    )

    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state_value,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{metadata.authorization_endpoint}?{urlencode(params)}"

    cfg = request.app.state.config
    response = RedirectResponse(url=authorize_url, status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=cookie_value,
        max_age=_STATE_MAX_AGE_SECONDS,
        httponly=True,
        secure=cfg.auth.cookie_secure,
        # Must be sent on the top-level GET navigation back from the IdP
        # (a cross-site redirect) -- SameSite=Strict would drop it there.
        samesite="lax",
        path="/",
    )
    return response


@sso_router.get("/{provider_id}/callback", name="sso_callback")
async def sso_callback(
    provider_id: str,
    request: Request,
    code: str | None = Query(default=None),
    oidc_provider_storage=Depends(get_oidc_provider_storage),
    user_identity_storage=Depends(get_user_identity_storage),
    user_storage=Depends(get_user_storage),
    storage_provider=Depends(get_storage_provider),
) -> RedirectResponse:
    secret = request.app.state.session_secret
    cookie_value = request.cookies.get(_STATE_COOKIE_NAME)
    state_payload = oidc.verify_state(cookie_value, secret, _STATE_MAX_AGE_SECONDS)
    if state_payload is None:
        raise _reject(400, "invalid_state", "missing, expired, or tampered state cookie")

    if not code:
        raise _reject(400, "missing_code")

    resolved_provider_id = state_payload.get("provider_id")
    nonce = state_payload.get("nonce")
    code_verifier = state_payload.get("code_verifier")
    return_to = _safe_return_to(state_payload.get("return_to"))
    if not isinstance(resolved_provider_id, str) or not isinstance(code_verifier, str):
        raise _reject(400, "invalid_state")

    # Resolve the provider from the STATE, never from the URL path param
    # -- the URL is unauthenticated user input.
    provider = await oidc_provider_storage.get(resolved_provider_id)
    if provider is None or not provider.enabled:
        raise _reject(404, "provider_not_found")

    try:
        metadata = await oidc.discover(provider.discovery_url)
        redirect_uri = str(request.url_for("sso_callback", provider_id=provider.id))
        token_response = await oidc.exchange_code(
            metadata=metadata,
            provider=provider,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        id_token = token_response.get("id_token")
        if not id_token:
            raise oidc.OidcError("token endpoint response is missing id_token")
        jwks = await oidc.fetch_jwks(metadata.jwks_uri)
        claims = await oidc.validate_id_token(
            id_token, provider=provider, metadata=metadata, jwks=jwks, nonce=nonce,
        )
    except oidc.OidcError as exc:
        raise _reject(400, "sso_validation_failed", str(exc)) from exc

    subject = claims["sub"]
    email = claims.get("email") if claims.get("email_verified") is True else None

    user = await _resolve_or_provision_user(
        storage_provider=storage_provider,
        user_storage=user_storage,
        identity_storage=user_identity_storage,
        provider_id=provider.id,
        subject=subject,
        email=email,
    )

    response = RedirectResponse(url=return_to, status_code=302)
    response.delete_cookie(key=_STATE_COOKIE_NAME, path="/")
    _set_session_cookie(request, response, user, src=provider.id)
    logger.info(
        "sso.callback success provider_id=%s user_id=%s", provider.id, user.id,
    )
    return response


__all__ = ["sso_router"]
