"""Authentication gate for the Prometheus ``/metrics`` mount (architecture review A-11).

``/metrics`` is a bare ASGI app mounted outside every router, so no auth dependency ever ran on it and an anonymous ``GET`` on the shipped
ingress answered 200. The registry carries workspace, provider, profile, model, tool and worker identifiers and how busy each is: an
inventory of the deployment for anyone who can reach the host.

:class:`MetricsGate` wraps the mount. :class:`~primer.api.middleware.auth.AuthMiddleware` runs first for the whole app, including mounts,
and leaves the authenticated user on ``scope["state"]`` (cookie, or ``Authorization: Bearer <api token>``, which is what a Prometheus
``authorization`` block sends). The gate then requires that user to be an ``admin``:

* no authenticated user: ``401`` problem+json with ``WWW-Authenticate: Bearer`` and ``extensions.error`` ``auth_required``;
* a user below admin: ``403`` problem+json with ``extensions.error`` ``forbidden_role`` (the two codes every other auth refusal in the API
  carries, so a client or a log filter that keys on them needs no special case for ``/metrics``);
* an admin: the request goes on to the metrics app unchanged;
* a websocket scope: refused (closed before it is accepted) whoever sent it. The Prometheus app speaks only http, but the gate must not
  depend on that. Any other scope kind (``lifespan``, which a mount is never sent) passes through: refusing it would stall startup.

With auth disabled the middleware injects its synthetic admin, so the endpoint is open exactly as the rest of the API is. A deployment whose
network already protects the port sets ``observability.metrics_public: true``: the mount is still wrapped (there is one code path), and the
gate lets every request through.

A refused request is answered here, so the body of a refusal carries no metric.
"""

from __future__ import annotations

from starlette.requests import Request

from primer.api.errors import _problem_for_status, _problem_response
from primer.model.user import User


class MetricsGate:
    """ASGI wrapper that lets only an authenticated admin through to the metrics app."""

    def __init__(self, app, *, public: bool = False) -> None:
        self._app = app
        self._public = public

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if self._public or kind not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()  # the connect message the server sends first; closing before accepting makes the server answer 403
            await send({"type": "websocket.close", "code": 1008})
            return
        user = getattr(scope.get("state"), "user", None)
        if isinstance(user, User) and user.role == "admin":
            await self._app(scope, receive, send)
            return
        if not isinstance(user, User):
            status, code, detail, headers = (
                401,
                "auth_required",
                "Reading /metrics needs a signed-in admin: send the session cookie, or an admin's API token as a bearer token.",
                {"WWW-Authenticate": 'Bearer realm="primer"'},
            )
        else:
            status, code, detail, headers = 403, "forbidden_role", "Reading /metrics needs the admin role.", None
        type_uri, title = _problem_for_status(status)
        response = _problem_response(
            request=Request(scope), status=status, type_uri=type_uri, title=title, detail=detail,
            extensions={"error": code}, headers=headers,
        )
        await response(scope, receive, send)


__all__ = ["MetricsGate"]
