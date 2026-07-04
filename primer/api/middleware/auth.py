"""Cookie- and bearer-based auth middleware.

Pure-ASGI implementation so the same auth logic runs for both ``http``
and ``websocket`` scopes. Two paths populate the same scope state:

1. **Cookie** (primary, existing): Reads the signed ``primer_session``
   cookie, re-fetches the user from storage, populates
   ``scope.state.user`` + ``.principal``. Cookie sessions implicitly
   carry full user authority — ``scope.state.api_token`` stays ``None``.

2. **Bearer fallback**: If the cookie path didn't authenticate, look for
   ``Authorization: Bearer <token>``. The token is hashed (sha256) and
   looked up in :class:`ApiToken` storage. Revoked / expired tokens are
   rejected. On success, populates ``scope.state.user`` + ``.principal``
   AND ``.api_token`` (so :func:`primer.api.deps.require_scope` can
   distinguish bearer from cookie auth).

Both ``request.state.user`` (HTTP) and ``websocket.state.user`` (WS) read
through the same scope state, so handlers see a consistent view.

We re-fetch the user every request so a deleted/disabled account can't
keep using a still-valid cookie or token. The hot-path cost is one
indexed read per auth path.

The ``last_used_at`` update on bearer auth is fire-and-forget via
:func:`asyncio.create_task` — best-effort, doesn't block the request.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from http.cookies import SimpleCookie

from starlette.datastructures import State

from primer.auth.api_tokens import PLAINTEXT_PREFIX, hash_token
from primer.auth.tokens import verify_session
from primer.model.principal import Principal
from primer.model.user import User


logger = logging.getLogger(__name__)


# Synthetic operator used when auth is disabled (config.auth.enabled is
# False, or no app config is present). Injecting it into request.state.user
# lets ``require_auth`` accept the request so /v1/* routes run
# unauthenticated as documented, instead of returning 401 everywhere.
# Built once with a fixed timestamp so it stays deterministic; the
# password_hash is a non-verifiable placeholder so this account can never
# authenticate via password.
_AUTH_DISABLED_USER = User(
    id="system",
    username="system",
    password_hash="!auth-disabled",
    created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    role="admin",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthMiddleware:
    """Pure-ASGI middleware. Populates scope state from cookie or bearer.

    Does NOT itself short-circuit on unauth — routers / WS handlers use
    :func:`primer.api.deps.require_auth` (HTTP) or a manual close (WS).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Initialise scope state so downstream callers can rely on the
        # attributes being present even when the user isn't authenticated.
        # Use Starlette's State (attribute-access dict) so request.state.x
        # and websocket.state.x both resolve via the same scope object.
        state = scope.get("state")
        if not isinstance(state, State):
            state = State()
            scope["state"] = state
        state.user = None
        state.principal = None
        state.api_token = None
        state.actor = None

        app_state = scope.get("app").state if scope.get("app") is not None else None
        if app_state is None:
            await self.app(scope, receive, send)
            return

        config = getattr(app_state, "config", None)
        if config is None or not config.auth.enabled:
            # Auth disabled (or unconfigured): inject the synthetic system
            # user so require_auth accepts the request and routes run
            # unauthenticated. principal/api_token stay None; actor becomes
            # a system Principal so RBAC gates reading state.actor treat the
            # request as fully authorised.
            state.user = _AUTH_DISABLED_USER
            state.actor = Principal(
                type="system",
                id=_AUTH_DISABLED_USER.id,
                display=_AUTH_DISABLED_USER.username,
                role=_AUTH_DISABLED_USER.role,
                source="system",
            )
            await self.app(scope, receive, send)
            return

        storage_provider = getattr(app_state, "storage_provider", None)
        if storage_provider is None:
            await self.app(scope, receive, send)
            return

        # 1. Cookie path (primary).
        user = await self._try_cookie_auth(scope, app_state, config, storage_provider)
        api_token = None

        # 2. Bearer fallback — only when cookie didn't authenticate.
        if user is None:
            user, api_token = await self._try_bearer_auth(scope, storage_provider)

        # A disabled account is treated as unauthenticated so deactivation
        # takes effect on the very next request (spec §9), uniformly across
        # REST, WebSocket, and MCP (all read scope.state.user).
        if user is not None and user.disabled:
            user = None
            api_token = None

        if user is not None:
            state.user = user
            state.principal = user.username
            state.api_token = api_token
            if api_token is not None:
                # Bearer path: the resolved ``user`` is the token OWNER, so
                # the actor carries the owner's role but is typed api_token.
                state.actor = Principal(
                    type="api_token",
                    id=api_token.id,
                    display=api_token.name,
                    role=user.role,
                    source="internal",
                )
            else:
                # Cookie path: full user authority.
                state.actor = Principal(
                    type="user",
                    id=user.id,
                    display=user.username,
                    role=user.role,
                    source="local",
                )

        await self.app(scope, receive, send)

    async def _try_cookie_auth(self, scope, app_state, config, storage_provider):
        """Existing cookie path. Returns the User or None."""
        secret = getattr(app_state, "session_secret", None)
        if not secret:
            return None

        token = _read_cookie(scope, config.auth.cookie_name)
        if not token:
            return None

        max_age = config.auth.session_ttl_days * 86400
        payload = verify_session(
            token=token, secret=secret, max_age_seconds=max_age,
        )
        if payload is None:
            return None

        try:
            from primer.model.user import User
            user_storage = storage_provider.get_storage(User)
            user = await user_storage.get(payload.user_id)
        except Exception:  # noqa: BLE001
            logger.exception("auth middleware: user lookup failed")
            return None

        return user

    async def _try_bearer_auth(self, scope, storage_provider):
        """Bearer fallback. Returns (User, ApiToken) or (None, None)."""
        bearer = _read_bearer(scope.get("headers", ()))
        if not bearer or not bearer.startswith(PLAINTEXT_PREFIX):
            return None, None

        try:
            from primer.model.api_token import ApiToken
            from primer.model.user import User

            th = hash_token(bearer)
            api_token = await self._find_by_hash(storage_provider, th)
            if api_token is None:
                return None, None
            if api_token.revoked_at is not None:
                return None, None
            if (
                api_token.expires_at is not None
                and api_token.expires_at <= _utcnow()
            ):
                return None, None

            user_storage = storage_provider.get_storage(User)
            user = await user_storage.get(api_token.user_id)
            if user is None:
                return None, None
        except Exception:  # noqa: BLE001
            logger.exception("auth middleware: bearer lookup failed")
            return None, None

        # Fire-and-forget last_used_at update.
        try:
            asyncio.create_task(
                self._touch_last_used(storage_provider, api_token)
            )
        except RuntimeError:
            # No running loop — extremely rare in ASGI; silently skip.
            logger.debug(
                "touch_last_used skipped: no running event loop",
            )

        return user, api_token

    async def _find_by_hash(self, storage_provider, token_hash: str):
        """Look up an ApiToken by its sha256 hash. Returns the row or None."""
        from primer.model.api_token import ApiToken
        from primer.model.storage import OffsetPage, Op
        from primer.storage.q import Q

        storage = storage_provider.get_storage(ApiToken)
        predicate = Q(ApiToken).where_op("token_hash", Op.EQ, token_hash).build()
        page = await storage.find(predicate, OffsetPage(offset=0, length=1))
        items = list(page.items)
        return items[0] if items else None

    async def _touch_last_used(self, storage_provider, api_token) -> None:
        """Best-effort update of api_token.last_used_at. Never raises."""
        from primer.model.api_token import ApiToken

        try:
            updated = api_token.model_copy(update={"last_used_at": _utcnow()})
            storage = storage_provider.get_storage(ApiToken)
            await storage.update(updated)
        except Exception:  # noqa: BLE001
            logger.debug(
                "touch_last_used failed for %s",
                getattr(api_token, "id", "?"),
                exc_info=True,
            )


def _read_cookie(scope, name: str) -> str | None:
    """Pull a single cookie value out of the raw ASGI scope headers."""
    for k, v in scope.get("headers", ()):
        if k == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:
                return None
            morsel = jar.get(name)
            return morsel.value if morsel is not None else None
    return None


def _read_bearer(headers) -> str | None:
    """Pull an ``Authorization: Bearer <token>`` value out of ASGI headers.

    Works for both http and websocket scopes (both pass the same
    raw-headers tuple shape). Returns the token string (no scheme
    prefix, trimmed) or None.
    """
    for k, v in headers:
        if k == b"authorization":
            try:
                decoded = v.decode("latin-1")
            except Exception:
                return None
            parts = decoded.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
                return token or None
            return None
    return None
