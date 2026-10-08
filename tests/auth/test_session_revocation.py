"""Session cookies can be revoked (SEC-05).

Each user carries a session generation, ``User.session_epoch``, and every
signed cookie carries the generation it was minted under. The auth
middleware already re-reads the user on every request, so it compares the
two there (no extra read) and treats a cookie from an older generation as
no cookie at all.

Declared rule: a password change, an admin password reset and
``POST /v1/auth/logout-all`` bump the generation and so end EVERY session
of that user; a plain logout only clears the caller's own cookie. A
legacy cookie signed before the generation existed counts as generation 0.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from itsdangerous import URLSafeTimedSerializer

from primer.auth.tokens import SessionPayload, sign_session, verify_session
from primer.model.storage import OffsetPage
from primer.model.user import User

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

_COOKIE = "primer_session"


def _replay(app, cookie: str) -> httpx.AsyncClient:
    """A fresh client presenting ``cookie``, as an attacker holding a stolen copy would."""
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(_COOKIE, cookie)
    return c


async def _authenticated(app, cookie: str) -> bool:
    async with _replay(app, cookie) as c:
        status = await c.get("/v1/auth/status")
        assert status.status_code == 200, status.text
        return status.json()["authenticated"]


async def _register_admin(client) -> str:
    r = await client.post("/v1/auth/register", json={"username": "alice", "password": "supersecret"})
    assert r.status_code == 200, r.text
    return r.cookies[_COOKIE]


async def _user(fake_storage_provider, username: str) -> User:
    page = await fake_storage_provider.get_storage(User).list(OffsetPage(offset=0, length=50))
    return next(u for u in page.items if u.username == username)


# ---- the token ----------------------------------------------------------------


def test_the_cookie_carries_the_session_epoch() -> None:
    secret = "x" * 32
    token = sign_session(user_id="u1", username="alice", secret=secret, epoch=3)
    payload = verify_session(token=token, secret=secret, max_age_seconds=60)
    assert payload == SessionPayload(user_id="u1", username="alice", epoch=3)


def test_a_legacy_cookie_without_an_epoch_counts_as_epoch_zero() -> None:
    secret = "x" * 32
    legacy = URLSafeTimedSerializer(secret, salt="primer.session.v1").dumps(
        {"uid": "u1", "username": "alice", "src": "local"}
    )
    payload = verify_session(token=legacy, secret=secret, max_age_seconds=60)
    assert payload is not None
    assert payload.epoch == 0


def test_a_malformed_epoch_is_rejected() -> None:
    secret = "x" * 32
    forged_shape = URLSafeTimedSerializer(secret, salt="primer.session.v1").dumps(
        {"uid": "u1", "username": "alice", "ep": "1"}
    )
    assert verify_session(token=forged_shape, secret=secret, max_age_seconds=60) is None


def test_a_new_user_starts_at_epoch_zero() -> None:
    from datetime import datetime, timezone

    u = User(id="u1", username="a", created_at=datetime.now(timezone.utc))
    assert u.session_epoch == 0


# ---- the flows ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_password_change_ends_every_other_session(client, app, fake_storage_provider):
    stolen = await _register_admin(client)
    assert await _authenticated(app, stolen)

    r = await client.post(
        "/v1/auth/change-password",
        json={"current_password": "supersecret", "new_password": "newsecret123"},
    )
    assert r.status_code == 200, r.text

    # The copy taken before the change is dead...
    assert not await _authenticated(app, stolen)
    async with _replay(app, stolen) as c:
        assert (await c.get("/v1/admin/users")).status_code == 401
    # ...while the caller got a fresh cookie and stays signed in.
    fresh = r.cookies.get(_COOKIE)
    assert fresh and fresh != stolen
    assert await _authenticated(app, fresh)
    assert (await _user(fake_storage_provider, "alice")).session_epoch == 1


@pytest.mark.asyncio
async def test_an_admin_password_reset_ends_the_users_sessions(client, app):
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    assert created.status_code == 201, created.text
    bob_id = created.json()["id"]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        assert login.status_code == 200, login.text
        bob_cookie = login.cookies[_COOKIE]
    assert await _authenticated(app, bob_cookie)

    reset = await client.patch(f"/v1/admin/users/{bob_id}", json={"generate_password": True})
    assert reset.status_code == 200, reset.text

    assert not await _authenticated(app, bob_cookie)
    # The admin's own session is untouched: only bob's generation moved.
    assert (await client.get("/v1/auth/status")).json()["authenticated"] is True


@pytest.mark.asyncio
async def test_an_admin_edit_without_a_password_keeps_the_users_sessions(client, app):
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        bob_cookie = login.cookies[_COOKIE]

    edit = await client.patch(f"/v1/admin/users/{bob_id}", json={"email": "bob@example.com"})
    assert edit.status_code == 200, edit.text
    assert await _authenticated(app, bob_cookie)


@pytest.mark.asyncio
async def test_logout_clears_only_this_cookie(client, app, fake_storage_provider):
    """The declared rule: logout does NOT end the user's other sessions."""
    await _register_admin(client)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = await other.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
        other_cookie = login.cookies[_COOKIE]

    out = await client.post("/v1/auth/logout")
    assert out.status_code == 204
    assert (await client.get("/v1/auth/status")).json()["authenticated"] is False
    assert await _authenticated(app, other_cookie)
    assert (await _user(fake_storage_provider, "alice")).session_epoch == 0


@pytest.mark.asyncio
async def test_logout_all_ends_every_session_of_the_user(client, app, fake_storage_provider):
    mine = await _register_admin(client)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = await other.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
        other_cookie = login.cookies[_COOKIE]

    r = await client.post("/v1/auth/logout-all")
    assert r.status_code == 204, r.text

    assert not await _authenticated(app, mine)
    assert not await _authenticated(app, other_cookie)
    assert (await client.get("/v1/auth/status")).json()["authenticated"] is False
    assert (await _user(fake_storage_provider, "alice")).session_epoch == 1
    # Signing in again works and mints a cookie of the new generation.
    again = await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
    assert again.status_code == 200
    assert await _authenticated(app, again.cookies[_COOKIE])


@pytest.mark.asyncio
async def test_logout_all_needs_a_session(client):
    r = await client.post("/v1/auth/logout-all")
    assert r.status_code == 401
