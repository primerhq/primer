"""Cookie-based auth middleware.

Reads the ``primer_session`` cookie from every request, verifies its
signature + age via :func:`primer.auth.tokens.verify_session`, and
re-fetches the user row from storage. On success populates:

* ``request.state.user``      → :class:`primer.model.user.User`
* ``request.state.principal`` → ``user.username`` (string)

On any failure the state attributes are set to ``None`` — the middleware
does NOT itself return 401. That's the router's job via the
:func:`primer.api.deps.require_auth` dependency, so endpoints can opt
out (login/register/status/health/metrics).

We re-fetch the user every request so a deleted/disabled user can't
keep using a still-valid cookie. The hot-path cost is one indexed read
per request — negligible against the rest of an API handler.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from primer.auth.tokens import verify_session


logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Populate ``request.state.user`` / ``.principal`` from a signed cookie."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.user = None
        request.state.principal = None

        app: FastAPI = request.app
        config = getattr(app.state, "config", None)
        secret = getattr(app.state, "session_secret", None)

        # Auth disabled (config.auth.enabled=False) or app not fully
        # configured: leave state unset. Routers' require_auth dep will
        # 401 if it's enforced.
        if config is None or not config.auth.enabled or not secret:
            return await call_next(request)

        cookie_name = config.auth.cookie_name
        token = request.cookies.get(cookie_name)
        if not token:
            return await call_next(request)

        max_age = config.auth.session_ttl_days * 86400
        payload = verify_session(
            token=token, secret=secret, max_age_seconds=max_age,
        )
        if payload is None:
            return await call_next(request)

        # Cookie valid; re-fetch user from storage so a deleted account
        # can't keep using a not-yet-expired cookie.
        storage_provider = getattr(app.state, "storage_provider", None)
        if storage_provider is None:
            return await call_next(request)

        try:
            from primer.model.user import User
            user_storage = storage_provider.get_storage(User)
            user = await user_storage.get(payload.user_id)
        except Exception:  # noqa: BLE001
            logger.exception("auth middleware: user lookup failed")
            return await call_next(request)

        if user is None:
            return await call_next(request)

        request.state.user = user
        request.state.principal = user.username
        return await call_next(request)
