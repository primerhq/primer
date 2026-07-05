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

Authenticated router (``sso_authed_router``, ``require_user`` applied at
``include_router`` time in ``_app_routes.py``) — account linking + the
caller's linked-identity list:

* ``GET /{provider_id}/link``      — same Authorization Code + PKCE
  flow as ``/{provider_id}/login``, but requires an active session and
  stamps the signed state with ``mode="link"`` plus the CURRENT user's
  id (never a URL/query param). Reuses the exact same
  ``redirect_uri`` (and hence the same ``/callback`` route) as
  ``/login`` — the fresh-login vs. account-linking branch lives
  entirely in the signed state, so the provider only needs ONE
  registered redirect URI.
* ``/callback`` (shared) — when the verified state's ``mode`` is
  ``"link"``, the id_token is validated with the same rigor as a
  regular login, then the resulting ``UserIdentity`` is attached to the
  user id carried in the state — never a new user, never a session
  swap. ``(provider_id, sub)`` already linked to a DIFFERENT user is a
  409 (never reassigned); already linked to the SAME user is a no-op
  success (idempotent — no 500 on the unique index).
* ``GET /identities``              — the caller's own linked identities.
* ``DELETE /identities/{id}``      — unlink one of the caller's own
  identities; another user's identity id is a masked 404 (never reveals
  existence).

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
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from primer.api.deps import (
    get_oidc_provider_storage,
    get_storage_provider,
    get_user_identity_storage,
    get_user_storage,
)
from primer.api.routers.auth import _set_session_cookie
from primer.auth import oidc
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.oidc import OidcProvider, UserIdentity
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.q import Q


logger = logging.getLogger(__name__)

sso_router = APIRouter(prefix="/auth/sso", tags=["auth", "sso"])

# Authenticated account-linking + linked-identity list/unlink surface.
# Mounted with ``dependencies=[Depends(require_user)]`` at include-router
# time (see ``_app_routes.py``) -- kept as a SEPARATE router from
# ``sso_router`` (rather than per-route ``dependencies=``) so the public
# login surface can never accidentally pick up an auth requirement, and
# vice versa.
sso_authed_router = APIRouter(prefix="/auth/sso", tags=["auth", "sso"])

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


class SsoIdentityOut(BaseModel):
    id: str
    provider_id: str
    provider_name: str
    subject: str
    email: str | None = None
    created_at: datetime


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

    # Defense-in-depth clamp: sso_default_access is unconstrained str | None
    # from system_state (validated at the admin-settings write path, but
    # never trusted here) — a misconfigured "admin" must never let the JIT
    # path auto-provision an admin account.
    role = state.sso_default_access if state.sso_default_access in ("restricted", "user") else "restricted"
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


async def _link_identity(
    *,
    identity_storage,
    target_user_id: str,
    provider_id: str,
    subject: str,
    email: str | None,
) -> UserIdentity:
    """Attach ``(provider_id, subject)`` to *target_user_id*. Never provisions.

    * Already linked to *target_user_id* -- idempotent no-op, returns the
      existing row (re-linking the same account must not 500 on the
      unique index).
    * Already linked to a DIFFERENT user -- raises 409. Identities are
      never stolen/reassigned by a link attempt.
    * Not linked anywhere -- creates the row.

    Mirrors the race-safety shape of :func:`_resolve_or_provision_user`:
    the existence check is check-then-act, so a concurrent link/login
    for the same ``(provider_id, subject)`` can still race between the
    check and the ``create`` -- the DB-level unique constraint on
    ``(provider_id, subject)`` is the actual backstop, and we catch
    :class:`ConflictError` and re-resolve rather than letting it 500.
    """
    existing = await _find_identity(identity_storage, provider_id, subject)
    if existing is not None:
        if existing.user_id != target_user_id:
            raise _reject(
                409, "identity_already_linked",
                "this identity is already linked to a different account",
            )
        return existing

    try:
        return await identity_storage.create(
            UserIdentity(
                user_id=target_user_id,
                provider_id=provider_id,
                subject=subject,
                email=email,
                created_at=datetime.now(timezone.utc),
            )
        )
    except ConflictError:
        winner = await _find_identity(identity_storage, provider_id, subject)
        if winner is None:
            # Should not happen (the conflict proves a row exists) -- fail
            # closed rather than silently dropping the link attempt.
            raise _reject(409, "sso_race_unresolved") from None
        if winner.user_id != target_user_id:
            raise _reject(
                409, "identity_already_linked",
                "this identity is already linked to a different account",
            ) from None
        return winner


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


async def _begin_oidc_flow(
    *,
    provider_id: str,
    request: Request,
    return_to: str | None,
    storage,
    extra_state: dict | None = None,
) -> RedirectResponse:
    """Shared Authorization Code + PKCE kickoff for ``/login`` and ``/link``.

    Discovers the provider, mints a fresh PKCE pair / state / nonce, and
    stashes them (plus ``extra_state``, when given) in the signed,
    short-lived, HttpOnly state cookie before 302ing to the provider's
    ``authorization_endpoint``. ``extra_state`` is how ``/link`` stamps
    ``mode="link"`` + the current session's user id onto the state
    without duplicating any of this crypto -- see the module docstring.
    """
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

    # Same physical redirect_uri for both /login and /link -- the
    # fresh-login vs. account-linking branch lives entirely in the
    # signed state (see sso_callback), so the provider only needs ONE
    # registered redirect URI.
    redirect_uri = str(request.url_for("sso_callback", provider_id=provider.id))

    secret = request.app.state.session_secret
    state_payload = {
        "provider_id": provider.id,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "return_to": safe_return_to,
    }
    if extra_state:
        state_payload.update(extra_state)
    cookie_value = oidc.sign_state(state_payload, secret)

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


@sso_router.get("/{provider_id}/login")
async def sso_login(
    provider_id: str,
    request: Request,
    return_to: str | None = Query(default=None),
    storage=Depends(get_oidc_provider_storage),
) -> RedirectResponse:
    return await _begin_oidc_flow(
        provider_id=provider_id, request=request, return_to=return_to, storage=storage,
    )


@sso_authed_router.get("/{provider_id}/link")
async def sso_link(
    provider_id: str,
    request: Request,
    return_to: str | None = Query(default=None),
    storage=Depends(get_oidc_provider_storage),
) -> RedirectResponse:
    """Authenticated equivalent of ``/login`` -- links a provider identity.

    ``require_user`` runs at include-router time (see ``_app_routes.py``),
    so by the time this handler executes ``request.state.user`` is always
    a non-restricted, authenticated :class:`User` -- reading it here is
    just a typed accessor, not a defensive re-check. The user id is
    stamped into the SIGNED state, never trusted from the URL/query.
    """
    user: User = request.state.user
    return await _begin_oidc_flow(
        provider_id=provider_id, request=request, return_to=return_to, storage=storage,
        extra_state={"mode": "link", "user_id": user.id},
    )


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
    mode = state_payload.get("mode")
    if not isinstance(resolved_provider_id, str) or not isinstance(code_verifier, str):
        raise _reject(400, "invalid_state")
    if mode == "link" and not isinstance(state_payload.get("user_id"), str):
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

    response = RedirectResponse(url=return_to, status_code=302)
    response.delete_cookie(key=_STATE_COOKIE_NAME, path="/")

    if mode == "link":
        # Authenticated account linking (Task 7). The target user comes
        # STRICTLY from the signed state stamped at /link time -- never
        # from this (unauthenticated-at-the-HTTP-layer) callback request.
        # Never mint a session here: the caller is already logged in, and
        # linking must not re-auth them as anyone -- same or otherwise.
        target_user_id = state_payload["user_id"]
        # Re-check the target still exists and isn't disabled: the user
        # could have been deleted/disabled in the (short) window between
        # /link and the browser returning from the IdP. Same invariant
        # the login path enforces (see the module docstring).
        target_user = await user_storage.get(target_user_id)
        if target_user is None or target_user.disabled:
            raise _reject(403, "account_disabled")
        identity = await _link_identity(
            identity_storage=user_identity_storage,
            target_user_id=target_user_id,
            provider_id=provider.id,
            subject=subject,
            email=email,
        )
        logger.info(
            "sso.link_callback success provider_id=%s user_id=%s identity_id=%s",
            provider.id, target_user_id, identity.id,
        )
        return response

    user = await _resolve_or_provision_user(
        storage_provider=storage_provider,
        user_storage=user_storage,
        identity_storage=user_identity_storage,
        provider_id=provider.id,
        subject=subject,
        email=email,
    )
    _set_session_cookie(request, response, user, src=provider.id)
    logger.info(
        "sso.callback success provider_id=%s user_id=%s", provider.id, user.id,
    )
    return response


# ---------------------------------------------------------------------------
# Endpoints -- linked-identity list/unlink (authenticated, owner-scoped)
# ---------------------------------------------------------------------------


@sso_authed_router.get("/identities", response_model=list[SsoIdentityOut])
async def list_my_sso_identities(
    request: Request,
    identity_storage=Depends(get_user_identity_storage),
    provider_storage=Depends(get_oidc_provider_storage),
) -> list[SsoIdentityOut]:
    """The CALLER's own linked identities -- never another user's."""
    user: User = request.state.user
    predicate = Q(UserIdentity).where("user_id", user.id).build()

    rows: list[UserIdentity] = []
    offset = 0
    while True:
        page = await identity_storage.find(predicate, OffsetPage(offset=offset, length=_PAGE_SIZE))
        rows.extend(page.items)
        if len(page.items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    out: list[SsoIdentityOut] = []
    for row in rows:
        # Tolerate a deleted provider -- must not 500; fall back to the
        # raw id as a display name since the human-readable name is gone.
        provider = await provider_storage.get(row.provider_id)
        out.append(
            SsoIdentityOut(
                id=row.id,
                provider_id=row.provider_id,
                provider_name=provider.name if provider is not None else row.provider_id,
                subject=row.subject,
                email=row.email,
                created_at=row.created_at,
            )
        )
    return out


@sso_authed_router.delete("/identities/{identity_id}", status_code=204)
async def unlink_sso_identity(
    identity_id: str,
    request: Request,
    identity_storage=Depends(get_user_identity_storage),
):
    """Unlink one of the CALLER's own identities.

    Owner-scoped by ``request.state.user.id``: an identity id that
    doesn't exist, or that exists but belongs to a DIFFERENT user, gets
    the SAME masked 404 -- this endpoint must never let a caller probe
    for the existence of another user's identity rows.
    """
    user: User = request.state.user
    row = await identity_storage.get(identity_id)
    if row is None or row.user_id != user.id:
        raise _reject(404, "identity_not_found")

    try:
        await identity_storage.delete(identity_id)
    except NotFoundError:
        pass  # already gone -- unlink is idempotent from the caller's view
    logger.info(
        "sso.unlink success user_id=%s identity_id=%s provider_id=%s",
        user.id, identity_id, row.provider_id,
    )
    return JSONResponse(status_code=204, content=None)


__all__ = ["sso_router", "sso_authed_router"]
