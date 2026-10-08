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


# ---- review of #495: lost updates, bearer re-issue, SSO src, ep shapes ----------


import asyncio  # noqa: E402

from primer.api.routers import admin_users as _admin_users_router  # noqa: E402
from primer.api.routers import auth as _auth_router  # noqa: E402


@pytest.mark.parametrize("ep", [-1, True, False, 1.0, None, [1]])
def test_a_negative_bool_or_non_int_epoch_is_rejected(ep) -> None:
    secret = "x" * 32
    token = URLSafeTimedSerializer(secret, salt="primer.session.v1").dumps(
        {"uid": "u1", "username": "alice", "ep": ep}
    )
    assert verify_session(token=token, secret=secret, max_age_seconds=60) is None


@pytest.mark.asyncio
async def test_a_legacy_cookie_with_no_epoch_authenticates_against_epoch_zero(client, app):
    await _register_admin(client)
    user = await _user(app.state.storage_provider, "alice")
    legacy = URLSafeTimedSerializer(app.state.session_secret, salt="primer.session.v1").dumps(
        {"uid": user.id, "username": "alice", "src": "local"}
    )
    assert await _authenticated(app, legacy)
    # ...and is revoked like any other once the epoch moves.
    assert (await client.post("/v1/auth/logout-all")).status_code == 204
    assert not await _authenticated(app, legacy)


@pytest.mark.asyncio
async def test_an_admin_reset_with_an_explicit_password_ends_the_users_sessions(client, app):
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        bob_cookie = login.cookies[_COOKIE]
    reset = await client.patch(f"/v1/admin/users/{bob_id}", json={"password": "brandnewpass"})
    assert reset.status_code == 200, reset.text
    assert not await _authenticated(app, bob_cookie)


@pytest.mark.asyncio
async def test_change_password_keeps_the_sso_src_on_the_reissued_cookie(client, app):
    await _register_admin(client)
    user = await _user(app.state.storage_provider, "alice")
    sso_cookie = sign_session(
        user_id=user.id, username="alice", secret=app.state.session_secret, src="oidc-corp", epoch=0,
    )
    async with _replay(app, sso_cookie) as c:
        r = await c.post(
            "/v1/auth/change-password",
            json={"current_password": "supersecret", "new_password": "newsecret123"},
        )
    assert r.status_code == 200, r.text
    fresh = verify_session(token=r.cookies[_COOKIE], secret=app.state.session_secret, max_age_seconds=60)
    assert fresh is not None and fresh.src == "oidc-corp" and fresh.epoch == 1


@pytest.mark.asyncio
async def test_change_password_over_a_bearer_token_sets_no_cookie(client, app):
    await _register_admin(client)
    minted = await client.post("/v1/auth/tokens", json={"name": "cli", "scopes": []})
    assert minted.status_code in (200, 201), minted.text
    bearer = minted.json()["plaintext"]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {bearer}"},
    ) as c:
        r = await c.post(
            "/v1/auth/change-password",
            json={"current_password": "supersecret", "new_password": "newsecret123"},
        )
    assert r.status_code == 200, r.text
    assert "set-cookie" not in r.headers
    # The epoch still moved: the cookie sessions of the account are over.
    assert (await _user(app.state.storage_provider, "alice")).session_epoch == 1


def _hold(monkeypatch, module, name):
    """Replace ``module.name`` (an async callable) with one that blocks until released."""
    real = getattr(module, name)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*a, **k):
        entered.set()
        await release.wait()
        return await real(*a, **k)

    monkeypatch.setattr(module, name, held)
    return entered, release


@pytest.mark.asyncio
async def test_a_login_held_across_a_logout_all_does_not_undo_it(client, app, monkeypatch):
    """The login read the row at epoch 0; its write must not put epoch 0 back after logout-all made it 1."""
    stolen = await _register_admin(client)
    entered, release = _hold(monkeypatch, _auth_router, "verify_password")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = asyncio.create_task(
            other.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        assert (await client.post("/v1/auth/logout-all")).status_code == 204
        release.set()
        r = await asyncio.wait_for(login, 5)
    assert r.status_code == 200, r.text
    assert (await _user(app.state.storage_provider, "alice")).session_epoch == 1
    assert not await _authenticated(app, stolen)
    # The login that finished after the revocation got a cookie of the current epoch.
    assert await _authenticated(app, r.cookies[_COOKIE])


@pytest.mark.asyncio
async def test_an_admin_reset_held_across_a_logout_all_still_ends_the_newer_sessions(client, app, monkeypatch):
    """A stale admin write must not land on the epoch a logout-all already reached."""
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    entered, release = _hold(monkeypatch, _admin_users_router, "hash_password")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        reset = asyncio.create_task(
            client.patch(f"/v1/admin/users/{bob_id}", json={"password": "brandnewpass"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        assert (await bob.post("/v1/auth/logout-all")).status_code == 204
        relogin = await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        newer = relogin.cookies[_COOKIE]
        assert await _authenticated(app, newer)
        release.set()
        r = await asyncio.wait_for(reset, 5)
    assert r.status_code == 200, r.text
    assert (await _user(app.state.storage_provider, "bob")).session_epoch == 2
    assert not await _authenticated(app, newer)


@pytest.mark.asyncio
async def test_an_admin_edit_held_across_a_logout_all_does_not_undo_it(client, app, monkeypatch):
    """An email-only edit read the row before the logout-all; it must write only the email."""
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    storage = app.state.storage_provider.get_storage(User)
    real_get = storage.get
    entered, release = asyncio.Event(), asyncio.Event()
    first = {"done": False}

    async def held_get(id, **k):  # noqa: A002
        row = await real_get(id, **k)
        if id == bob_id and not first["done"]:
            first["done"] = True
            entered.set()
            await release.wait()
        return row

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = await bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        bob_cookie = login.cookies[_COOKIE]
        monkeypatch.setattr(storage, "get", held_get)
        edit = asyncio.create_task(
            client.patch(f"/v1/admin/users/{bob_id}", json={"email": "bob@example.com"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        monkeypatch.setattr(storage, "get", real_get)
        assert (await bob.post("/v1/auth/logout-all")).status_code == 204
        release.set()
        r = await asyncio.wait_for(edit, 5)
    assert r.status_code == 200, r.text
    stored = await _user(app.state.storage_provider, "bob")
    assert stored.session_epoch == 1
    assert stored.email == "bob@example.com"
    assert not await _authenticated(app, bob_cookie)


# ---- review round 2: the guards themselves ----------------------------------------


def _hold_first(monkeypatch, module, name):
    """Block only the FIRST call of ``module.name`` until released; later calls run straight through."""
    real = getattr(module, name)
    entered, release = asyncio.Event(), asyncio.Event()
    state = {"first": True}

    async def held(*a, **k):
        if state["first"]:
            state["first"] = False
            entered.set()
            await release.wait()
        return await real(*a, **k)

    monkeypatch.setattr(module, name, held)
    return entered, release


@pytest.mark.asyncio
async def test_a_login_held_across_a_password_change_is_refused(client, app, monkeypatch):
    """The login verified the OLD password; stamp_login's password_hash guard must refuse it afterwards."""
    await _register_admin(client)
    entered, release = _hold_first(monkeypatch, _auth_router, "verify_password")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        login = asyncio.create_task(
            other.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        changed = await client.post(
            "/v1/auth/change-password",
            json={"current_password": "supersecret", "new_password": "newsecret123"},
        )
        release.set()
        r = await asyncio.wait_for(login, 5)
    assert changed.status_code == 200, changed.text
    assert r.status_code == 401, r.text
    assert "set-cookie" not in r.headers


@pytest.mark.asyncio
async def test_a_login_held_across_an_admin_reset_is_refused(client, app, monkeypatch):
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    entered, release = _hold(monkeypatch, _auth_router, "verify_password")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = asyncio.create_task(
            bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        reset = await client.patch(f"/v1/admin/users/{bob_id}", json={"password": "brandnewpass"})
        assert reset.status_code == 200, reset.text
        release.set()
        r = await asyncio.wait_for(login, 5)
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_two_concurrent_password_changes_cannot_both_win(client, app, monkeypatch):
    """Both verified the same current password; the slower one must be refused, not overwrite the first."""
    from primer.auth.passwords import verify_password

    await _register_admin(client)
    entered, release = _hold_first(monkeypatch, _auth_router, "hash_password")
    first = asyncio.create_task(client.post(
        "/v1/auth/change-password",
        json={"current_password": "supersecret", "new_password": "firstnew123"},
    ))
    await asyncio.wait_for(entered.wait(), 5)
    async with _replay(app, client.cookies[_COOKIE]) as c2:
        second = await c2.post(
            "/v1/auth/change-password",
            json={"current_password": "supersecret", "new_password": "secondnew123"},
        )
    assert second.status_code == 200, second.text
    release.set()
    r = await asyncio.wait_for(first, 5)
    assert r.status_code == 401, r.text
    stored = await _user(app.state.storage_provider, "alice")
    assert await verify_password("secondnew123", stored.password_hash)
    assert stored.session_epoch == 1


@pytest.mark.asyncio
async def test_a_login_whose_user_is_deleted_mid_request_is_a_401(client, app, monkeypatch):
    await _register_admin(client)
    created = await client.post(
        "/v1/admin/users", json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    bob_id = created.json()["id"]
    entered, release = _hold(monkeypatch, _auth_router, "verify_password")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        login = asyncio.create_task(
            bob.post("/v1/auth/login", json={"username": "bob", "password": "bobpassword"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        await app.state.storage_provider.get_storage(User).delete(bob_id)
        release.set()
        r = await asyncio.wait_for(login, 5)
    assert r.status_code == 401, r.text
