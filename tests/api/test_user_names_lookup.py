"""The ids form of the user lookup (access-control plan, Phase 6 Task 3a): the console's owner and grantee badges
have ids (a row's ``access.owners`` / ``grants``), not usernames, so ``GET /v1/users/lookup?ids=`` names them.

Same minimal app as test_access_routes.py: the error handlers, a test-only middleware that turns an ``X-Test-User:
<id>:<role>`` header into ``request.state.actor``, the real SQLite provider and the router under test. The User
storage is wrapped in a counting proxy: the lookup is one batched query (``id IN (...)``), never one read per id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.api.errors import register_error_handlers
from primer.api.routers.access import users_lookup_router
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider


class _CountingFind:
    """Delegates to the real User storage and counts ``find`` (the batched-query pin)."""

    def __init__(self, real) -> None:
        self._real = real
        self.find_calls = 0

    def find(self, *args, **kwargs):
        self.find_calls += 1
        return self._real.find(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


# Same vocabulary as test_access_routes.py: ur is restricted and ud is disabled -
# the q lookup never offers them, but an owner may have been demoted (or the
# account locked), so the ids lookup must still name them.
_USERS = [
    ("u1", "alice", "user", False),
    ("u2", "bob", "user", False),
    ("u3", "carol", "user", False),
    ("ur", "alba", "restricted", False),
    ("ud", "albert", "user", True),
    ("ua", "root", "admin", False),
]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, _CountingFind]]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    users = provider.get_storage(User)
    for uid, name, role, disabled in _USERS:
        await users.create(
            User(id=uid, username=name, password_hash=None, created_at=datetime.now(UTC), role=role, disabled=disabled)
        )
    spy = _CountingFind(users)
    real_get_storage = provider.get_storage
    provider.get_storage = lambda m: spy if m is User else real_get_storage(m)

    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = provider
    app.state.provider_registry = object()  # get_storage_provider asserts the app was wired; nothing here reads it

    @app.middleware("http")
    async def _actor(request: Request, call_next):
        raw = request.headers.get("X-Test-User")
        if raw:
            uid, role = raw.split(":")
            request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
        else:
            request.state.actor = None
        return await call_next(request)

    app.include_router(users_lookup_router, prefix="/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, spy
    await provider.aclose()


def _as(uid: str, role: str = "user") -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{role}"}


async def test_names_resolve_a_batch_in_one_query(env):
    """One request names many ids in the order asked (the badge draws them in
    the row's own order), and the storage answers with ONE query - the ids form
    is a batched read, never one per id."""
    client, spy = env
    r = await client.get("/v1/users/lookup", params={"ids": "u2,u1,u3"}, headers=_as("u2"))
    assert r.status_code == 200
    assert [(u["id"], u["username"]) for u in r.json()] == [("u2", "bob"), ("u1", "alice"), ("u3", "carol")]
    assert set(r.json()[0]) == {"id", "username"}
    assert spy.find_calls == 1, "the ids lookup is one batched query, not one read per id"


async def test_names_deduplicate_ids(env):
    client, _ = env
    r = await client.get("/v1/users/lookup", params={"ids": "u1,u1, u2,"}, headers=_as("u1"))
    assert r.status_code == 200
    assert [u["id"] for u in r.json()] == ["u1", "u2"], "duplicates collapse and blank parts are dropped"


async def test_names_unknown_ids_are_absent(env):
    client, _ = env
    r = await client.get("/v1/users/lookup", params={"ids": "u1,ghost"}, headers=_as("u1"))
    assert r.status_code == 200
    assert [u["id"] for u in r.json()] == ["u1"], "an unknown id is simply absent, not an error"
    none = await client.get("/v1/users/lookup", params={"ids": "ghost,phantom"}, headers=_as("u1"))
    assert none.status_code == 200 and none.json() == []


async def test_names_resolve_disabled_and_restricted_users(env):
    """The q lookup never offers a disabled or restricted user (nothing can be
    shared to them), but the ids lookup must still name them: an owner may have
    been demoted or the account locked, and the console says who owns the row."""
    client, _ = env
    r = await client.get("/v1/users/lookup", params={"ids": "ur,ud,ua"}, headers=_as("u1"))
    assert r.status_code == 200
    assert [u["username"] for u in r.json()] == ["alba", "albert", "root"]
    # The response says the name only: no role, no disabled flag, nothing else.
    assert all(set(u) == {"id", "username"} for u in r.json())


async def test_names_refuse_restricted_callers(env):
    client, _ = env
    r = await client.get("/v1/users/lookup", params={"ids": "u1"}, headers=_as("ur", "restricted"))
    assert r.status_code == 403


async def test_names_cap_at_200_ids(env):
    client, spy = env
    two_hundred = ",".join(["u1"] + [f"m{i:03d}" for i in range(1, 200)])  # u1 + 199 unknown
    ok = await client.get("/v1/users/lookup", params={"ids": two_hundred}, headers=_as("u1"))
    assert ok.status_code == 200
    assert [u["id"] for u in ok.json()] == ["u1"]
    over = await client.get("/v1/users/lookup", params={"ids": two_hundred + ",m999"}, headers=_as("u1"))
    assert over.status_code == 422
