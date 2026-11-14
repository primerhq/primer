"""Cookie-based auth router.

Endpoints (all under ``/v1/auth``):

* ``GET  /status``    - returns ``{has_user, authenticated, username, principal}``.
                        Used by the UI to decide register vs login vs main
                        app on initial load. Public. It also says how the
                        request is signed in (``sign_in_method`` and, for
                        single sign-on, ``sign_in_provider``).
* ``POST /register``  — body ``{username, password}``. Only valid when no
                        user exists yet (single-user v1). 409 if any user
                        already registered; 422 on weak password. Sets
                        session cookie on success.
* ``POST /login``     — body ``{username, password}``. 401 on bad creds.
                        Sets session cookie on success. After repeated
                        failures for one username from one client address
                        it answers 429 ``too_many_attempts`` with a
                        ``Retry-After`` header (``primer.auth.throttle``;
                        in-process state).
* ``POST /logout``    — clears the session cookie. 204. Idempotent.

The router is registered without an auth guard so unauthenticated users
can hit register/login. The middleware (Commit 5) populates
``request.state.user`` if a valid cookie is present; this router uses
that to decide whether ``status.authenticated`` is True.
"""

from __future__ import annotations

from primer.api.route_tools import route_tool

import ipaddress
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from primer.api.deps import get_storage_provider, require_auth, require_user
from primer.api.errors import common_responses
from primer.api.user_default import ensure_default_for_request
from primer.auth.passwords import HashBusy, HashGate, hash_password, verify_password
from primer.auth.throttle import LoginThrottle
from primer.auth.tokens import sign_session
from primer.auth.user_writes import stamp_login, write_user_fields
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.oidc import OidcProvider
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.storage._predicate import FieldRef, Op, Predicate, Value
from primer.storage._patch import raw_generation
from primer.storage.cas import patch_if_checked
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["auth"])

_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MIN_PASSWORD_LEN = 8


# ---------------------------------------------------------------------------
# Request / response bodies
# ---------------------------------------------------------------------------


#: The longest password any door takes (login, register, change-password): argon2 hashes whatever it is given, so the length is the only bound on the
#: work one request can ask for besides the body limit, and no stored password is longer than a login accepts.
_MAX_PASSWORD_LEN = 4096


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=_MAX_PASSWORD_LEN)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=_MAX_PASSWORD_LEN)  # not enforcing min on login
    remember: bool = Field(
        default=True,
        description=(
            "If true (default), the session cookie carries a Max-Age "
            "and persists across browser restarts. If false, the cookie "
            "has no Max-Age so the browser drops it when closed."
        ),
    )


class WelcomeState(BaseModel):
    variant: Literal["admin", "user"]
    show: bool


class WelcomeRoleBody(BaseModel):
    role: Literal["admin", "user"] = Field(
        ..., description="The currently displayed role; a stale role is refused rather than acknowledging another variant.",
    )


class AuthStatus(BaseModel):
    has_user: bool = Field(..., description="True if at least one user exists.")
    authenticated: bool = Field(
        ...,
        description="True if the current request carries a valid session cookie.",
    )
    welcome: WelcomeState | None = Field(
        default=None,
        description="The real user's current role welcome; absent for signed-out, restricted and auth-disabled callers.",
    )
    username: str | None = Field(
        default=None,
        description="Logged-in user's username (only set when authenticated).",
    )
    role: str | None = Field(
        default=None,
        description="Logged-in user's role (only set when authenticated).",
    )
    principal: PrincipalRef | None = Field(
        default=None,
        description=(
            "The request actor's stable type, id and role, for console "
            "affordances that mirror server authorization. Unset when signed out."
        ),
    )
    must_change_password: bool = Field(
        default=False,
        description="True if the logged-in user must rotate their password "
        "before using the app.",
    )
    default_workspace_id: str | None = Field(
        default=None,
        description=(
            "The caller's own default workspace (access-control spec 6.7): the private workspace a full user gets at their first "
            "sign-in. For an auth-disabled install, the install-wide default. Unset when signed out, for a restricted user, "
            "and for an API token's owner who has not signed in yet."
        ),
    )
    setup_complete: bool = Field(
        default=False,
        description=(
            "True when this install is configured: an LLM provider, a model "
            "profile, and the S5 seeded objects all exist. Derived live; "
            "there is no stamp."
        ),
    )
    setup_missing: list[str] = Field(
        default_factory=list,
        description=(
            "Setup pieces that are still missing, in wizard order "
            "(llm_provider, model_profile, default_workspace, "
            "operator_agent, builder_agent, system_collection). Empty when "
            "setup_complete is true."
        ),
    )
    sign_in_method: Literal["password", "sso", "api_token", "auth_disabled"] | None = Field(
        default=None,
        description=(
            "How this request is signed in: 'password' (a password login), "
            "'sso' (a single sign-on login through sign_in_provider), "
            "'api_token' (a bearer API key) or 'auth_disabled' (auth is off; "
            "every request runs as the built-in system admin). Unset when not "
            "signed in."
        ),
    )
    sign_in_provider: str | None = Field(
        default=None,
        description=(
            "When sign_in_method is 'sso': the single sign-on provider's name, "
            "or its id when that provider no longer exists. Unset otherwise."
        ),
    )


class AuthOk(BaseModel):
    username: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_username(raw: str) -> str:
    return raw.strip().lower()


def _require_auth_enabled(request: Request) -> None:
    """Refuse session/state-signing auth flows when auth is disabled."""
    if request.app.state.config.auth.enabled:
        return
    raise HTTPException(
        status_code=400,
        detail={
            "error": "auth_disabled",
            "message": "This authentication flow is unavailable because authentication is disabled.",
        },
    )


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


def _login_throttle(request: Request) -> LoginThrottle:
    """The app's sign-in throttle, created on first use (one per app object, so every test app starts clean).

    In-process state: see :mod:`primer.auth.throttle` for what that means for a deployment with more than one API process.
    """
    state = request.app.state
    throttle = getattr(state, "login_throttle", None)
    if throttle is None:
        throttle = state.login_throttle = LoginThrottle()
    return throttle


#: Failed sign-ins one client address may make across ALL usernames before it backs off (the per-address budget; successes give theirs back).
LOGIN_ADDRESS_FREE_ATTEMPTS = 30


def _login_address_throttle(request: Request) -> LoginThrottle:
    """The per-address budget: failed logins of one address across usernames, which the per-(username, address) key never sees (a client that varies
    the username was never refused)."""
    state = request.app.state
    throttle = getattr(state, "login_address_throttle", None)
    if throttle is None:
        throttle = state.login_address_throttle = LoginThrottle(free_attempts=LOGIN_ADDRESS_FREE_ATTEMPTS)
    return throttle


def _password_change_throttle(request: Request) -> LoginThrottle:
    """The throttle of ``current_password`` checks in change-password, keyed on the user id (a stolen session or token was an unthrottled oracle)."""
    state = request.app.state
    throttle = getattr(state, "password_change_throttle", None)
    if throttle is None:
        throttle = state.password_change_throttle = LoginThrottle()
    return throttle


def _hash_gate(request: Request) -> HashGate:
    """The app's gate on password hashing and verification (``HASH_CONCURRENCY`` at once, a bounded queue behind them)."""
    state = request.app.state
    gate = getattr(state, "hash_gate", None)
    if gate is None:
        gate = state.hash_gate = HashGate()
    return gate


def _busy() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"error": "busy", "message": "too many sign-ins are being checked right now; try again in a moment"},
        headers={"Retry-After": "1"},
    )


def _too_many(wait: int, what: str) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "too_many_attempts",
            "message": f"too many {what}; try again in {wait} seconds",
            "retry_after_seconds": wait,
        },
        headers={"Retry-After": str(wait)},
    )


def _client_address(request: Request) -> str:
    """The peer address the server sees, as a throttle key. Deliberately NOT ``X-Forwarded-For``: a client writes that header, so keying on it
    would let an attacker pick a fresh key per attempt. Behind a proxy this is the real client only when uvicorn trusts the
    proxy (``FORWARDED_ALLOW_IPS``).

    An IPv6 client is keyed on its /64 (a host owns a whole /64 and can rotate addresses inside it for free); an IPv4-mapped IPv6 address is
    the IPv4 address (review of gaps-security, GS-40)."""
    host = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return host


def _set_session_cookie(
    request: Request,
    response: Response,
    user: User,
    *,
    remember: bool = True,
    src: str = "local",
) -> None:
    cfg = request.app.state.config.auth
    secret = request.app.state.session_secret
    token = sign_session(
        user_id=user.id, username=user.username, secret=secret, src=src,
        epoch=user.session_epoch,
    )
    # remember=False omits Max-Age so the browser treats it as a
    # session cookie (cleared on browser close). The token's signed
    # max-age stays at session_ttl_days either way — that's the
    # server-side upper bound; browser persistence is the only thing
    # remember actually toggles.
    max_age = cfg.session_ttl_days * 86400 if remember else None
    response.set_cookie(
        key=cfg.cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite=cfg.cookie_samesite,
        path="/",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _sign_in_of(request: Request) -> tuple[str | None, str | None]:
    """How the request is signed in, as ``(sign_in_method, sign_in_provider)``; ``(None, None)`` when it is not signed in.

    Read from ``request.state.actor``, the ``Principal`` the auth middleware resolved from the auth path that won (see
    :class:`primer.model.principal.Principal`): a ``system`` actor is auth switched off, an ``api_token`` actor is a bearer key, and
    a ``user`` actor is a cookie session whose ``source`` is ``"local"`` for a password login or the OIDC provider id for a single
    sign-on login (``primer/api/routers/sso.py`` mints that cookie with ``src=provider.id``). Only the SSO case reads storage, for the
    provider's name. A provider deleted since the login, or a read that fails, falls back to its id, so this public boot probe still
    answers.
    """
    actor = getattr(request.state, "actor", None)
    if actor is None:
        return None, None
    if actor.type == "system":
        return "auth_disabled", None
    if actor.type == "api_token":
        return "api_token", None
    if actor.type != "user":
        return None, None
    if actor.source == "local":
        return "password", None
    try:
        provider = await get_storage_provider(request).get_storage(OidcProvider).get(actor.source)
    except Exception:  # noqa: BLE001
        logger.exception("auth.status: reading SSO provider %s failed; reporting its id", actor.source)
        return "sso", actor.source
    return "sso", provider.name if provider is not None else actor.source


@auth_router.get("/status", response_model=AuthStatus)
@route_tool(scoped_id="auth__auth_status")
async def auth_status(request: Request) -> AuthStatus:
    """Probe used by the UI to pick register / login / main flow."""
    from primer.bootstrap.setup_state import evaluate_setup_state

    user = getattr(request.state, "user", None)
    has_user = await _has_any_user(request)
    setup = await evaluate_setup_state(get_storage_provider(request))
    sign_in_method, sign_in_provider = await _sign_in_of(request)
    actor = getattr(request.state, "actor", None)
    default_workspace_id: str | None = None
    if user is not None and actor is not None and actor.type == "user":
        # An existing cookie session: a user promoted from restricted since their last sign-in gets the workspace here.
        default_workspace_id = await ensure_default_for_request(request, user, fast=True)
    elif actor is not None and actor.type == "system":
        from primer.bootstrap.prime import resolve_default_workspace_id

        default_workspace_id = await resolve_default_workspace_id(get_storage_provider(request))
    return AuthStatus(
        default_workspace_id=default_workspace_id,
        has_user=has_user,
        authenticated=user is not None,
        welcome=_welcome_state(user) if request.app.state.config.auth.enabled else None,
        username=user.username if user is not None else None,
        role=user.role if user is not None else None,
        principal=(PrincipalRef.from_principal(actor) if actor is not None else None),
        must_change_password=(
            user.must_change_password if user is not None else False
        ),
        setup_complete=setup.complete,
        setup_missing=setup.missing,
        sign_in_method=sign_in_method,
        sign_in_provider=sign_in_provider,
    )


def _welcome_state(user: User | None) -> WelcomeState | None:
    if user is None or user.disabled or user.role not in ("admin", "user"):
        return None
    return WelcomeState(variant=user.role, show=user.role not in user.welcome_acked_roles)


async def _write_welcome(request: Request, user: User, role: Literal["admin", "user"], *, acknowledged: bool) -> WelcomeState:
    _require_auth_enabled(request)

    def check(current: User) -> None:
        if current.disabled or current.role not in ("admin", "user"):
            raise HTTPException(403, detail={"error": "forbidden_role", "message": "Your role does not allow this."})
        if current.role != role:
            raise HTTPException(409, detail={
                "error": "welcome_role_changed", "message": "Your role changed; refresh the welcome before trying again.",
            })

    def fields(current: User) -> dict:
        roles = current.welcome_acked_roles
        return {"welcome_acked_roles": roles | {role} if acknowledged else roles - {role}}

    provider = get_storage_provider(request)
    storage = provider.get_storage(User)
    for _ in range(8):
        current = await storage.get(user.id)
        if current is None:
            raise NotFoundError(f"User {user.id!r} does not exist")
        async with provider.transaction() as conn:
            # Ordinary role/disabled edits do not bump session_epoch. Obtain
            # the row lock with a value-preserving guarded write, then check
            # the CURRENT returned row and derive membership under that lock.
            # This also protects a role ABA without comparing JSON arrays.
            locked = await patch_if_checked(
                storage, user.id, {"session_epoch": current.session_epoch},
                where={"session_epoch": [raw_generation(current, "session_epoch")]}, conn=conn,
            )
            if locked is None:
                continue
            check(locked)
            saved = await write_user_fields(
                storage, user.id, fields, bump_epoch=False, check=check, conn=conn,
            )
        break
    else:
        raise ConflictError(f"User {user.id!r} kept changing; retry the welcome request")
    logger.info("auth.welcome %s username=%s role=%s", "acknowledged" if acknowledged else "reopened", saved.username, role)
    return WelcomeState(variant=role, show=role not in saved.welcome_acked_roles)


@auth_router.post(
    "/welcome/acknowledge", response_model=WelcomeState, responses=common_responses(400, 401, 403, 404, 409),
)
@route_tool(
    scoped_id="auth__acknowledge_welcome", required_role="user", read_only=False,
    purpose="Remember that you have seen the welcome for your current role.",
    use_when="After the welcome is shown. Only your current role is acknowledged; other roles and sign-in sessions are preserved.",
    preview_args=("body.role",),
)
async def acknowledge_welcome(body: WelcomeRoleBody, request: Request, user: User = Depends(require_user)) -> WelcomeState:
    """Acknowledge the authenticated person's current welcome without ending any sign-in session."""
    return await _write_welcome(request, user, body.role, acknowledged=True)


@auth_router.post(
    "/welcome/reopen", response_model=WelcomeState, responses=common_responses(400, 401, 403, 404, 409),
)
@route_tool(
    scoped_id="auth__reopen_welcome", required_role="user", read_only=False,
    purpose="Show your role's welcome and suggested prompts again.",
    use_when="When you want the welcome again. Clears only your current role's acknowledgement and changes no workspace or session.",
    preview_args=("body.role",),
)
async def reopen_welcome(body: WelcomeRoleBody, request: Request, user: User = Depends(require_user)) -> WelcomeState:
    """Reopen only the authenticated person's current role variant."""
    return await _write_welcome(request, user, body.role, acknowledged=False)


@auth_router.post(
    "/register", response_model=AuthOk, responses=common_responses(400, 409),
)
@route_tool(mapping_state="skip", skip_code="human_sign_in")
async def register(
    body: RegisterBody, request: Request, response: Response,
) -> AuthOk:
    """First-boot operator account creation. Returns 409 if a user
    already exists (single-user v1)."""
    _require_auth_enabled(request)
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
        # Single-user v1 register is only reachable when no user exists
        # yet (see the 409 guard above), so this is always the first
        # account — Layer 1 RBAC requires there's always an admin.
        role="admin",
    )
    storage = get_storage_provider(request).get_storage(User)
    await storage.create(user)
    _set_session_cookie(request, response, user)
    logger.info("auth.register success username=%s", username)
    return AuthOk(username=username)


@auth_router.post(
    "/login",
    response_model=AuthOk,
    responses=common_responses(400, 401, 429, 503),
)
@route_tool(mapping_state="skip", skip_code="human_sign_in")
async def login(
    body: LoginBody, request: Request, response: Response,
) -> AuthOk:
    """Verify password, set session cookie.

    Throttled per (username, client address) with an exponential backoff (``primer.auth.throttle``): an attempt made during the
    backoff is refused with 429 and a ``Retry-After`` header before the account is looked up, so the refusal is the same for a
    real and an invented username, and a correct password is refused too. The attempt is counted when it starts, because the
    password check below is slow and awaited.
    """
    # Auth-disabled requests already act as the synthetic system admin. The
    # real lifespan deliberately has no cookie-signing secret in this mode.
    _require_auth_enabled(request)

    username = _normalise_username(body.username)
    client = _client_address(request)
    throttle = _login_throttle(request)

    # The address budget first (failures across every username); the attempt it lets through is counted now and given back on a success or when
    # the (username, address) key refuses it.
    address_throttle = _login_address_throttle(request)
    address_wait = address_throttle.reserve("", client)
    if address_wait:
        if address_throttle.first_refusal("", client):
            logger.info("auth.login throttled address=%s retry_after=%ss", client, address_wait)
        raise _too_many(address_wait, "failed sign-in attempts from this address")

    wait = throttle.reserve(username, client)
    if wait:
        address_throttle.refund("", client)  # refused unexamined: not a failure of this address
        # One line per wait, not per refused request: a client hammering a locked key must not fill the log.
        if throttle.first_refusal(username, client):
            logger.info("auth.login throttled username=%s retry_after=%ss", username, wait)
        raise _too_many(wait, "sign-in attempts for this username")

    gate = _hash_gate(request)

    async def gated(work, *args):
        """``work(*args)`` (a hash or a verify) in one of the gate's slots; a full queue answers 503 and counts as no attempt at all."""
        try:
            async with gate.slot():
                return await work(*args)
        except HashBusy:
            throttle.refund(username, client)
            address_throttle.refund("", client)
            raise _busy() from None

    user = await _find_user_by_username(request, username)
    if user is None:
        # Hash a throwaway value to keep timing consistent with the
        # happy path (defence against username-enumeration via timing).
        await gated(hash_password, body.password)
        logger.info("auth.login fail (unknown user) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )

    if user.password_hash is None:
        # SSO-only account (no local password). Hash a throwaway value to
        # keep timing consistent with the happy path, then reject
        # indistinguishably from an unknown user.
        await gated(hash_password, body.password)
        logger.info("auth.login fail (no local password) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )

    if user.disabled:
        # Disabled account. Hash a throwaway value to keep timing
        # consistent, then reject indistinguishably from bad creds.
        await gated(hash_password, body.password)
        logger.info("auth.login fail (disabled) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )

    if not await gated(verify_password, body.password, user.password_hash):
        logger.info("auth.login fail (bad password) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )

    throttle.succeeded(username, client)
    address_throttle.refund("", client)  # a success is not a failure of this address

    # Stamp ONLY last_login_at (SEC-05 review): a whole-document write of the row read before the
    # slow password check would put back the session_epoch a concurrent sign-out-everywhere or
    # password change had moved, re-validating revoked cookies. The guard also refuses the login if
    # the password changed or the account was disabled while the hash was being verified.
    storage = get_storage_provider(request).get_storage(User)
    try:
        stored = await stamp_login(storage, user, at=datetime.now(timezone.utc))
    except NotFoundError:
        # The account was deleted while its password was being verified.
        stored = None
    if stored is None:
        logger.info("auth.login fail (changed during verify) username=%s", username)
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )
    # Signed with the epoch the row holds NOW, so a login that finishes after a revocation is valid.
    _set_session_cookie(request, response, stored, remember=body.remember)
    await ensure_default_for_request(request, stored)
    logger.info(
        "auth.login success username=%s remember=%s", username, body.remember,
    )
    return AuthOk(username=username)


@auth_router.post("/logout", status_code=204)
@route_tool(mapping_state="skip", skip_code="human_sign_in")
async def logout(request: Request, response: Response) -> None:
    """Clear this browser's session cookie. Idempotent.

    Ends only the caller's own session (SEC-05, declared rule): the user's
    other sessions, and any copy of this cookie taken before, stay valid
    until they expire. ``POST /v1/auth/logout-all`` ends all of them.
    """
    cfg = request.app.state.config.auth
    response.delete_cookie(key=cfg.cookie_name, path="/")


@auth_router.post(
    "/logout-all", status_code=204, responses=common_responses(401),
)
@route_tool(mapping_state="skip", skip_code="human_sign_in")
async def logout_all(
    request: Request,
    response: Response,
    user: User = Depends(require_auth),
) -> None:
    """Sign out everywhere: end every session cookie of the caller's account.

    Increments ``User.session_epoch``, so every cookie minted before (this
    one, other browsers', any stolen copy) is rejected from the next request
    on, and clears this browser's cookie. API tokens are not sessions and
    are unaffected; revoke them separately.
    """
    cfg = request.app.state.config.auth
    storage = get_storage_provider(request).get_storage(User)
    if await storage.get(user.id) is not None:
        # None only for the synthetic user of an auth-disabled deployment, which has no sessions.
        # Field-scoped compare-and-set on the epoch: no other field of the row is written.
        await write_user_fields(storage, user.id, {}, bump_epoch=True)
    response.delete_cookie(key=cfg.cookie_name, path="/")
    logger.info("auth.logout_all username=%s", user.username)


class ChangePasswordArgs(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=_MAX_PASSWORD_LEN)
    new_password: str = Field(..., min_length=_MIN_PASSWORD_LEN, max_length=_MAX_PASSWORD_LEN)


@auth_router.post("/change-password", response_model=AuthOk)
@route_tool(mapping_state="skip", skip_code="human_sign_in")
async def change_password(
    body: ChangePasswordArgs,
    request: Request,
    response: Response,
    user: User = Depends(require_auth),
) -> AuthOk:
    """Change the authenticated user's password.

    Verifies ``current_password`` against the stored hash, rehashes
    ``new_password``, and clears ``must_change_password`` so the forced-
    rotation gate stops firing. A wrong current password (or an SSO-only
    account whose ``password_hash`` is ``None``, via the verify_password
    guard) is rejected with 401 ``invalid_credentials``.

    A successful change ends every session of the account (SEC-05): the
    session epoch is incremented, so cookies minted before the change,
    including any stolen copy, stop working, and the caller is handed a
    fresh cookie of the new epoch so it stays signed in.
    """
    # ``current_password`` is a password oracle for whoever holds the session or token: throttled per user (not per address: the holder can change it).
    change_throttle = _password_change_throttle(request)
    wait = change_throttle.reserve(user.id, "")
    if wait:
        if change_throttle.first_refusal(user.id, ""):
            logger.info("auth.change_password throttled username=%s retry_after=%ss", user.username, wait)
        raise _too_many(wait, "password change attempts")
    gate = _hash_gate(request)
    try:
        async with gate.slot():
            verified = await verify_password(body.current_password, user.password_hash)
    except HashBusy:
        change_throttle.refund(user.id, "")
        raise _busy() from None
    if not verified:
        logger.info(
            "auth.change_password fail (bad current) username=%s",
            user.username,
        )
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_credentials",
                "message": "The username or password is wrong.",
            },
        )
    change_throttle.succeeded(user.id, "")
    verified_hash = user.password_hash
    try:
        async with gate.slot():
            new_hash = await hash_password(body.new_password)
    except HashBusy:
        raise _busy() from None

    def _still_verified(current: User) -> None:
        # The password the caller proved is no longer the stored one: another change won the race.
        if current.password_hash != verified_hash:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "invalid_credentials",
                    "message": "The username or password is wrong.",
                },
            )

    storage = get_storage_provider(request).get_storage(User)
    saved = await write_user_fields(
        storage, user.id,
        {"password_hash": new_hash, "must_change_password": False},
        bump_epoch=True, check=_still_verified,
    )
    if getattr(request.state, "api_token", None) is None:
        # Only a cookie session gets a fresh cookie; a bearer-token caller never had one.
        actor = getattr(request.state, "actor", None)
        _set_session_cookie(
            request, response, saved, src=getattr(actor, "source", None) or "local",
        )
    logger.info("auth.change_password success username=%s", user.username)
    return AuthOk(username=user.username)
