"""Cut off a connection that is already open when its account stops being valid (security ticket 01a11b97).

:class:`~primer.api.middleware.auth.AuthMiddleware` reads the user when a request or WebSocket OPENS. A terminal shell, a tap SSE stream or an MCP
streamable-HTTP GET stream can outlive that read by hours, so disabling a compromised account, bumping its ``session_epoch`` (sign-out-everywhere,
a password change), demoting it or revoking its API token used to leave every connection it already held fully alive.

Design. The check is DATA-driven, not event-driven: the account's own rows in storage are the source of truth, so it works across API processes
with no registry and no bus, and it covers every long-lived handler, present and future, in one place. A request that is still running after
``auth.revalidate_interval_s`` gets a watcher beside it (a short request never does, and costs nothing extra); every interval the watcher re-reads
the user, and the API token for a bearer connection, and ends the connection when:

* the user is gone or disabled;
* a cookie session's ``session_epoch`` is no longer the one it was opened with (a cookie's epoch equals the user's at open, the middleware checked);
* the user's role changed (a demoted admin must not keep the authority the connection was gated with; reconnecting re-runs the role gate);
* a bearer connection's API token is gone, revoked or expired.

Ending a connection cancels the downstream app (its ``finally`` blocks run: a terminal tears its PTY down) and then closes it properly: a WebSocket
gets close code 4401 ``auth_revoked`` (the code the handlers already use for "authentication required"), an HTTP stream is completed rather than left
half-written, so the client sees a clean end and its reconnect meets the ordinary 401. A storage error is tolerated for ``_MAX_UNKNOWN_CHECKS - 1``
consecutive checks (one blip must not drop every open shell), then fails closed. The worst case between an account change and the close is one interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Consecutive checks that could not read storage before the connection is closed anyway.
_MAX_UNKNOWN_CHECKS = 3
# How long a cancelled app gets to unwind (its ``finally`` blocks) before the connection is closed over it anyway.
_CANCEL_GRACE_S = 5.0

WS_CLOSE_AUTH_REVOKED = 4401


@dataclass(frozen=True)
class AuthSnapshot:
    """Who a connection was opened as, as the middleware resolved it."""

    user_id: str
    role: str
    session_epoch: int
    api_token_id: str | None

    @classmethod
    def of(cls, user: Any, api_token: Any) -> "AuthSnapshot":
        return cls(
            user_id=user.id,
            role=user.role,
            session_epoch=user.session_epoch,
            api_token_id=api_token.id if api_token is not None else None,
        )


async def account_verdict(storage_provider: Any, snapshot: AuthSnapshot) -> tuple[str, str]:
    """``("valid" | "revoked" | "unknown", why)`` for the account behind an open connection."""
    from primer.model.api_token import ApiToken
    from primer.model.user import User

    try:
        user = await storage_provider.get_storage(User).get(snapshot.user_id)
    except Exception:  # noqa: BLE001 - a storage error is "unknown", not "revoked"
        logger.warning("open connection: could not re-read user %s", snapshot.user_id, exc_info=True)
        return "unknown", "storage_error"
    if user is None:
        return "revoked", "user_gone"
    if user.disabled:
        return "revoked", "user_disabled"
    if snapshot.api_token_id is None and user.session_epoch != snapshot.session_epoch:
        return "revoked", "session_epoch_moved"
    if user.role != snapshot.role:
        return "revoked", "role_changed"
    if snapshot.api_token_id is not None:
        try:
            token = await storage_provider.get_storage(ApiToken).get(snapshot.api_token_id)
        except Exception:  # noqa: BLE001
            logger.warning("open connection: could not re-read api token %s", snapshot.api_token_id, exc_info=True)
            return "unknown", "storage_error"
        if token is None:
            return "revoked", "token_gone"
        if token.revoked_at is not None:
            return "revoked", "token_revoked"
        if token.expires_at is not None and token.expires_at <= datetime.now(timezone.utc):
            return "revoked", "token_expired"
    return "valid", ""


class _Tracker:
    """Wraps ``send`` to remember how far the response got, so it can be ended properly when the app is cancelled."""

    def __init__(self, send: Callable[[dict], Awaitable[None]]) -> None:
        self._send = send
        self.http_started = False
        self.http_done = False
        self.ws_closed = False

    async def send(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "http.response.start":
            self.http_started = True
        elif kind == "http.response.body" and not message.get("more_body", False):
            self.http_done = True
        elif kind in ("websocket.close", "websocket.http.response.start"):
            self.ws_closed = True
        await self._send(message)

    async def finish_revoked(self, scope_type: str) -> None:
        """End the connection the app left open: a close frame for a WebSocket, a completed (or, if nothing was sent yet, a 401) response."""
        try:
            if scope_type == "websocket":
                if not self.ws_closed:
                    await self._send({"type": "websocket.close", "code": WS_CLOSE_AUTH_REVOKED, "reason": "auth_revoked"})
            elif self.http_done:
                return
            elif self.http_started:
                await self._send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                body = json.dumps({
                    "type": "/errors/unauthorized", "title": "Unauthorized", "status": 401,
                    "detail": "This session is no longer valid. Sign in again.",
                }).encode()
                await self._send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode())],
                })
                await self._send({"type": "http.response.body", "body": body, "more_body": False})
        except Exception:  # noqa: BLE001 - the peer may already be gone; nothing is left to tell it
            logger.debug("open connection: could not send the closing frame", exc_info=True)


async def run_with_revalidation(
    app: Any,
    scope: dict,
    receive: Any,
    send: Any,
    *,
    storage_provider: Any,
    snapshot: AuthSnapshot,
    interval_s: float,
) -> None:
    """Run ``app`` and end the connection if its account stops being valid while it is open (see the module docstring)."""
    tracker = _Tracker(send)
    app_task = asyncio.ensure_future(app(scope, receive, tracker.send))
    try:
        done, _ = await asyncio.wait({app_task}, timeout=interval_s)
        unknown_in_a_row = 0
        while not done:
            verdict, why = await account_verdict(storage_provider, snapshot)
            if verdict == "unknown":
                unknown_in_a_row += 1
                if unknown_in_a_row >= _MAX_UNKNOWN_CHECKS:
                    verdict, why = "revoked", "storage_unavailable"
            else:
                unknown_in_a_row = 0
            if verdict == "revoked":
                logger.warning(
                    "closing an open %s connection of user %s: %s", scope["type"], snapshot.user_id, why,
                )
                app_task.cancel()
                await asyncio.wait({app_task}, timeout=_CANCEL_GRACE_S)
                if app_task.done() and not app_task.cancelled():
                    app_task.exception()       # retrieved, so it is never logged as "never retrieved"
                await tracker.finish_revoked(scope["type"])
                return
            done, _ = await asyncio.wait({app_task}, timeout=interval_s)
        app_task.result()                      # the app's own exception, if it raised, propagates as it would have
    except asyncio.CancelledError:
        # The server cancelled this request (the client went away, or shutdown): the app beneath must not be left running.
        app_task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait({app_task})
        raise
