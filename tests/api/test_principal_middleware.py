"""Principal value object + AuthMiddleware actor-resolution (RBAC)."""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest
from starlette.datastructures import State

# Convention: keep the API-suite import shape uniform (unused fixtures here).
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.conftest import _FakeStorageProvider

from primer.api.middleware.auth import AuthMiddleware
from primer.auth.api_tokens import hash_token, mint_plaintext
from primer.auth.tokens import sign_session
from primer.model.api_token import ApiToken
from primer.model.principal import Principal
from primer.model.user import User


def _make_app_obj(storage, *, enabled: bool = True, secret: str = "test-secret"):
    auth_cfg = types.SimpleNamespace(
        enabled=enabled,
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
    """Run AuthMiddleware over a crafted HTTP scope; return the resolved State."""
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


def test_principal_serialization_shape():
    p = Principal(
        type="user", id="user-1", display="alice", role="admin", source="local",
    )
    assert p.model_dump() == {
        "type": "user",
        "id": "user-1",
        "display": "alice",
        "role": "admin",
        "source": "local",
    }
    # role is optional and defaults to None.
    bare = Principal(type="system", id="system", display="system", source="system")
    assert bare.role is None


@pytest.mark.asyncio
async def test_middleware_resolves_system_actor_when_auth_disabled():
    storage = _FakeStorageProvider()
    app_obj = _make_app_obj(storage, enabled=False)
    state = await _drive(app_obj, headers=[])
    assert state.actor is not None
    assert state.actor.type == "system"
    assert state.actor.source == "internal"
    assert state.actor.role is None


@pytest.mark.asyncio
async def test_middleware_resolves_user_actor_from_cookie():
    storage = _FakeStorageProvider()
    user = User(
        id="user-a",
        username="alice",
        password_hash="!x",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    await storage.get_storage(User).create(user)
    app_obj = _make_app_obj(storage)
    token = sign_session(user_id="user-a", username="alice", secret="test-secret")
    headers = [(b"cookie", f"primer_session={token}".encode("latin-1"))]
    state = await _drive(app_obj, headers)
    assert state.actor.type == "user"
    assert state.actor.id == "user-a"
    assert state.actor.display == "alice"
    assert state.actor.role == "user"
    assert state.actor.source == "local"


@pytest.mark.asyncio
async def test_middleware_resolves_api_token_actor_from_bearer():
    storage = _FakeStorageProvider()
    owner = User(
        id="user-owner",
        username="owner",
        password_hash="!x",
        created_at=datetime.now(timezone.utc),
        role="admin",
    )
    await storage.get_storage(User).create(owner)
    plaintext = mint_plaintext()
    tok = ApiToken(
        id="at-1",
        user_id="user-owner",
        name="ci-token",
        token_hash=hash_token(plaintext),
        prefix=plaintext[:8],
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await storage.get_storage(ApiToken).create(tok)
    app_obj = _make_app_obj(storage)
    headers = [(b"authorization", f"Bearer {plaintext}".encode("latin-1"))]
    state = await _drive(app_obj, headers)
    assert state.actor.type == "api_token"
    assert state.actor.id == "at-1"
    assert state.actor.display == "ci-token"
    assert state.actor.role == "admin"  # owner's role
    assert state.actor.source == "internal"
