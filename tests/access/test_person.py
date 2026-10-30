"""The Person, and the user behind every principal (access-control spec 5.1; plan Task 2).

``Person`` is the identity every access decision runs against. For a live
request it is derived from ``request.state.actor`` (an API-token caller is
the token's owner user, never the token itself); for a persisted initiator
it is derived from a ``PrincipalRef``. Admins and the auth-disabled system
principal act as ``admin``; a live request with no actor acts as
``restricted``; a missing or unattributed ref acts as ``user`` with no
``user_id``, so nothing is readable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import Request

from primer.access.person import Person, person_from_ref, person_from_request
from primer.api.middleware.auth import AuthMiddleware
from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.auth.tokens import sign_session
from primer.model.api_token import ApiToken
from primer.model.principal import Principal, PrincipalRef
from primer.model.user import User
from tests.conftest import _FakeStorageProvider

_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def _app(*, auth_enabled: bool, storage, secret: str | None = None):
    """A minimal ASGI app object the middleware can read config from."""

    class _AuthCfg:
        enabled = auth_enabled
        cookie_name = "primer_session"
        session_ttl_days = 30

    class _Cfg:
        auth = _AuthCfg()

    class _State:
        config = _Cfg()
        storage_provider = storage
        session_secret = secret

    class _App:
        state = _State()

    return _App()


async def _run_middleware(scope) -> object:
    """Drive the real AuthMiddleware over ``scope``; return its state."""
    seen: dict = {}

    async def inner(scope, receive, send):
        seen["state"] = scope["state"]

    async def receive():
        return {}

    async def send(_):
        return None

    await AuthMiddleware(inner)(scope, receive, send)
    return seen["state"]


@pytest.mark.asyncio
async def test_person_from_api_token_request_is_the_token_owner():
    """A bearer caller is the user who owns the token, not the token."""
    storage = _FakeStorageProvider()
    user = User(id="u7", username="owner7", role="user", created_at=_NOW)
    await storage.get_storage(User).create(user)
    plaintext = mint_plaintext()
    await storage.get_storage(ApiToken).create(
        ApiToken(
            id="at-u7",
            user_id=user.id,
            name="ci",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=_NOW,
        )
    )
    app = _app(auth_enabled=True, storage=storage)
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"authorization", f"Bearer {plaintext}".encode())],
    }
    state = await _run_middleware(scope)
    assert state.actor.type == "api_token" and state.actor.id == "at-u7"
    # The actor carries the token's owner user, and the person is that user.
    assert person_from_request(Request(scope)) == Person("u7", "user")


@pytest.mark.asyncio
async def test_person_from_cookie_request():
    """A cookie caller is the signed-in user with their own role."""
    storage = _FakeStorageProvider()
    user = User(id="u3", username="cookie3", role="admin", created_at=_NOW)
    await storage.get_storage(User).create(user)
    secret = "test-session-secret"
    token = sign_session(user_id=user.id, username=user.username, secret=secret)
    app = _app(auth_enabled=True, storage=storage, secret=secret)
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"cookie", f"primer_session={token}".encode())],
    }
    state = await _run_middleware(scope)
    assert state.actor.type == "user" and state.actor.user_id == "u3"
    assert person_from_request(Request(scope)) == Person("u3", "admin")


@pytest.mark.asyncio
async def test_auth_disabled_is_system_admin():
    """Auth-disabled installs act as the system principal (an admin)."""
    app = _app(auth_enabled=False, storage=_FakeStorageProvider())
    scope = {"type": "http", "app": app, "headers": []}
    state = await _run_middleware(scope)
    assert state.actor.type == "system"
    person = person_from_request(Request(scope))
    assert person == Person(None, "system") and person.is_admin


@pytest.mark.asyncio
async def test_unauthenticated_request_is_restricted():
    """A live request with no actor has no user and no role but the gate."""
    app = _app(auth_enabled=True, storage=_FakeStorageProvider())
    scope = {"type": "http", "app": app, "headers": []}
    state = await _run_middleware(scope)
    assert state.actor is None
    assert person_from_request(Request(scope)) == Person(None, "restricted")


def test_legacy_user_ref_without_user_id_uses_id():
    """A persisted user ref from before user_id landed: the id IS the user."""
    ref = PrincipalRef(type="user", id="u9", display="u9", role="user", source="local")
    assert person_from_ref(ref) == Person("u9", "user")


def test_unattributed_ref_has_no_user():
    """The unattributed stand-in (and a missing ref) name no user."""
    assert person_from_ref(PrincipalRef.unattributed()) == Person(None, "user")
    assert person_from_ref(None) == Person(None, "user")


def test_from_principal_copies_user_id():
    """The persisted projection carries the actor's user id across."""
    actor = Principal(
        type="api_token", id="at-1", display="t", role="user",
        source="internal", user_id="u7",
    )
    ref = PrincipalRef.from_principal(actor)
    assert ref.user_id == "u7"
    assert person_from_ref(ref) == Person("u7", "user")


def test_person_role_properties():
    assert Person(None, "system").is_admin
    assert Person("u1", "admin").is_admin
    assert Person("u1", "user").is_full_user
    assert not Person(None, "restricted").is_admin
    assert not Person(None, "restricted").is_full_user


# ---- person_for_tool: the person behind a tool call (Task 8) -------------------------------------------------


def test_person_for_tool_uses_the_run_initiator_when_there_is_a_context():
    from primer.access.person import person_for_tool
    from primer.model.yield_ import ToolContext

    ref = PrincipalRef(type="api_token", id="tok-1", display="t", role="user", source="local", user_id="u9")
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=ref)
    assert person_for_tool(ctx) == Person("u9", "user")


def test_person_for_tool_without_a_context_is_the_mcp_request_actor():
    from primer.access.person import person_for_tool
    from primer.mcp.server import current_actor

    actor = Principal(type="api_token", id="tok-2", display="t", role="admin", source="local", user_id="u7")
    token = current_actor.set(actor)
    try:
        assert person_for_tool(None) == Person("u7", "admin")
    finally:
        current_actor.reset(token)


def test_person_for_tool_with_nothing_known_reads_only_public_rows():
    from primer.access.person import person_for_tool

    assert person_for_tool(None) == Person(None, "user")
