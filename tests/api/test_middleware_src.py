"""AuthMiddleware resolves Principal.source from the session's `src` claim
(Layer 2 task 5)."""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest
from starlette.datastructures import State

from tests.conftest import _FakeStorageProvider

from primer.api.middleware.auth import AuthMiddleware
from primer.auth.tokens import sign_session
from primer.model.user import User


def _make_app_obj(storage, *, secret: str = "test-secret"):
    auth_cfg = types.SimpleNamespace(
        enabled=True,
        cookie_name="primer_session",
        session_ttl_days=7,
    )
    app_state = types.SimpleNamespace(
        config=types.SimpleNamespace(auth=auth_cfg),
        storage_provider=storage,
        session_secret=secret,
    )
    return types.SimpleNamespace(state=app_state)


async def _drive(app_obj, headers):
    captured: dict = {}

    async def _capture(scope, receive, send):
        captured["state"] = scope["state"]

    async def _receive():
        return {"type": "http.request"}

    async def _send(_msg):
        return None

    scope = {
        "type": "http",
        "app": app_obj,
        "headers": headers,
        "state": State(),
    }
    await AuthMiddleware(_capture)(scope, _receive, _send)
    return captured["state"]


async def _make_user(storage, *, user_id="user-a", username="alice"):
    user = User(
        id=user_id,
        username=username,
        password_hash="!x",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    await storage.get_storage(User).create(user)
    return user


@pytest.mark.asyncio
async def test_sso_cookie_resolves_actor_source_to_provider_id():
    storage = _FakeStorageProvider()
    await _make_user(storage)
    app_obj = _make_app_obj(storage)
    token = sign_session(
        user_id="user-a", username="alice", secret="test-secret",
        src="oidc-provider-1",
    )
    headers = [(b"cookie", f"primer_session={token}".encode("latin-1"))]
    state = await _drive(app_obj, headers)
    assert state.actor is not None
    assert state.actor.type == "user"
    assert state.actor.source == "oidc-provider-1"


@pytest.mark.asyncio
async def test_password_cookie_resolves_actor_source_to_local():
    storage = _FakeStorageProvider()
    await _make_user(storage)
    app_obj = _make_app_obj(storage)
    token = sign_session(user_id="user-a", username="alice", secret="test-secret")
    headers = [(b"cookie", f"primer_session={token}".encode("latin-1"))]
    state = await _drive(app_obj, headers)
    assert state.actor is not None
    assert state.actor.source == "local"
