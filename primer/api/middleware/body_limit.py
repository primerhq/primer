"""App-wide request-body size limit (FS-05).

Without it every route buffered whatever it was sent: FastAPI reads the
body before any dependency (auth included) runs, so even an anonymous
client could make the process hold an arbitrarily large body in memory.

``BodySizeLimitMiddleware`` is a plain ASGI middleware. It refuses a
declared ``Content-Length`` over the route's cap before the route runs,
and wraps ``receive`` to count the bytes actually delivered, so a chunked
body without a Content-Length is cut off at the cap too. The counting
refusal is a Starlette ``HTTPException`` (413): FastAPI re-raises an
``HTTPException`` from body parsing instead of turning it into a 400, so
the registered handler renders it as problem+json.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from primer.api.config import RequestLimitsConfig
from primer.api.errors import payload_too_large_response
from primer.api.version import API_VERSION


class _BodyTooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail={
                "code": "payload_too_large",
                "message": f"request body exceeds the {limit}-byte limit for this route",
                "limit_bytes": limit,
            },
        )
        self.limit = limit


# (method, path pattern, config field) for the routes that legitimately take big bodies.
_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("PUT", rf"^/{API_VERSION}/workspaces/[^/]+/files$", "workspace_file_max_body_bytes"),
    ("POST", rf"^/{API_VERSION}/services/[^/]+/versions$", "service_publish_max_body_bytes"),
)


def body_limit_options(limits: RequestLimitsConfig) -> dict[str, object]:
    """The keyword arguments ``BodySizeLimitMiddleware`` is installed with, from the config."""
    return {
        "max_body_bytes": limits.max_body_bytes,
        "overrides": tuple(
            (method, pattern, getattr(limits, field)) for method, pattern, field in _OVERRIDES
        ),
    }


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        overrides: Sequence[tuple[str, str, int]] = (),
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self._overrides = [(m, re.compile(p), limit) for m, p, limit in overrides]

    def _limit_for(self, method: str, path: str) -> int:
        for m, pattern, limit in self._overrides:
            if m == method and pattern.match(path):
                return limit
        return self.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope["method"], scope["path"])

        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await payload_too_large_response(Request(scope), limit_bytes=limit)(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            # A route that read the stream outside FastAPI's body parsing let the refusal escape the
            # exception handlers; answer it here unless a response is already under way.
            if started:
                raise
            await payload_too_large_response(Request(scope), limit_bytes=limit)(scope, receive, send)


__all__ = ["BodySizeLimitMiddleware", "body_limit_options"]
