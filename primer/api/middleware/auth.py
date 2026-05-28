"""Cookie-based auth middleware.

Pure-ASGI implementation so the same auth logic runs for both ``http``
and ``websocket`` scopes. Reads the ``primer_session`` cookie, verifies
its signature + age, re-fetches the user from storage, and populates:

* ``scope["state"]["user"]``      → :class:`primer.model.user.User`
* ``scope["state"]["principal"]`` → ``user.username`` (string)

Both ``request.state.user`` (HTTP) and ``websocket.state.user`` (WS) read
through the same scope state, so handlers see a consistent view.

We re-fetch the user every request so a deleted/disabled account can't
keep using a still-valid cookie. The hot-path cost is one indexed read.
"""

from __future__ import annotations

import logging
from http.cookies import SimpleCookie

from starlette.datastructures import State

from primer.auth.tokens import verify_session


logger = logging.getLogger(__name__)


class AuthMiddleware:
    """Pure-ASGI middleware. Populates scope state from the session cookie.

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

        app_state = scope.get("app").state if scope.get("app") is not None else None
        if app_state is None:
            await self.app(scope, receive, send)
            return

        config = getattr(app_state, "config", None)
        secret = getattr(app_state, "session_secret", None)
        if config is None or not config.auth.enabled or not secret:
            await self.app(scope, receive, send)
            return

        token = _read_cookie(scope, config.auth.cookie_name)
        if not token:
            await self.app(scope, receive, send)
            return

        max_age = config.auth.session_ttl_days * 86400
        payload = verify_session(
            token=token, secret=secret, max_age_seconds=max_age,
        )
        if payload is None:
            await self.app(scope, receive, send)
            return

        storage_provider = getattr(app_state, "storage_provider", None)
        if storage_provider is None:
            await self.app(scope, receive, send)
            return

        try:
            from primer.model.user import User
            user_storage = storage_provider.get_storage(User)
            user = await user_storage.get(payload.user_id)
        except Exception:  # noqa: BLE001
            logger.exception("auth middleware: user lookup failed")
            await self.app(scope, receive, send)
            return

        if user is None:
            await self.app(scope, receive, send)
            return

        state.user = user
        state.principal = user.username
        await self.app(scope, receive, send)


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
