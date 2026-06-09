"""Auth-disabled escape hatch — AuthMiddleware must inject a synthetic
system user so protected /v1/* routes stay reachable.

Regression for: disabling auth (``config.auth.enabled = False``) left
``request.state.user = None``, so every ``require_auth`` route returned
401, making the documented "running unauthenticated" mode produce a
fully inaccessible API.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.model.user import User


@pytest.mark.asyncio
async def test_auth_disabled_route_reachable(app):
    """With auth disabled, a protected route returns 200, not 401."""
    app.state.config = app.state.config.model_copy(
        update={"auth": app.state.config.auth.model_copy(update={"enabled": False})}
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/v1/agents")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_injects_synthetic_user():
    """The disabled branch sets state.user to a system User."""
    from primer.api.middleware.auth import AuthMiddleware

    captured = {}

    async def inner(scope, receive, send):
        captured["user"] = scope["state"].user
        captured["principal"] = scope["state"].principal
        captured["api_token"] = scope["state"].api_token

    mw = AuthMiddleware(inner)

    class _Auth:
        enabled = False

    class _Cfg:
        auth = _Auth()

    class _AppState:
        config = _Cfg()

    class _App:
        state = _AppState()

    scope = {"type": "http", "app": _App(), "headers": []}

    async def receive():
        return {}

    async def send(_):
        return None

    await mw(scope, receive, send)
    assert isinstance(captured["user"], User)
    assert captured["principal"] is None
    assert captured["api_token"] is None
