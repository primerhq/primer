"""The access sub-resource and the user lookup (access-control plan, Phase 1 Task 6).

A minimal app: the error handlers, a test-only middleware that turns an ``X-Test-User: <id>:<role>`` header into
``request.state.actor``, a tiny owned model on a real SQLite provider, and the routers under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.access.service import AccessView
from primer.api.deps import get_storage_provider
from primer.api.errors import register_error_handlers
from primer.api.routers.access import make_access_router, users_lookup_router
from primer.model.access import Access, Owned
from primer.model.common import Identifiable
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider


class Gadget(Identifiable, Owned):
    name: str = "g"


class Plain(Identifiable, Owned):
    """An owned model whose router does not allow public tools."""


_USERS = [
    ("u1", "alice", "user", False),
    ("u2", "bob", "user", False),
    ("u3", "carol", "user", False),
    ("u4", "Alfred", "user", False),
    ("ur", "alba", "restricted", False),
    ("ud", "albert", "user", True),
    ("ua", "root", "admin", False),
    ("u_", "a_%x", "user", False),
]


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        users = provider.get_storage(User)
        for uid, name, role, disabled in _USERS:
            await users.create(
                User(id=uid, username=name, password_hash=None, created_at=datetime.now(UTC), role=role, disabled=disabled)
            )
        gadgets = provider.get_storage(Gadget)
        await gadgets.create(Gadget(id="g1", access=Access(owners=["u1"], grants={"u2": "read"})))
        await provider.get_storage(Plain).create(Plain(id="p1", access=Access(owners=["u1"])))

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

        def _gadgets(request: Request):
            return get_storage_provider(request).get_storage(Gadget)

        def _plains(request: Request):
            return get_storage_provider(request).get_storage(Plain)

        app.include_router(
            make_access_router(model_cls=Gadget, plural="gadgets", storage_dep=_gadgets, tag="t", allow_public_tools=True),
            prefix="/v1",
        )
        app.include_router(make_access_router(model_cls=Plain, plural="plains", storage_dep=_plains, tag="t"), prefix="/v1")
        app.include_router(users_lookup_router, prefix="/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        await provider.aclose()


def _as(uid: str, role: str = "user") -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{role}"}


async def test_owner_reads_the_access_view(client):
    r = await client.get("/v1/gadgets/g1/access", headers=_as("u1"))
    assert r.status_code == 200
    view = AccessView.model_validate(r.json())
    assert [o.model_dump() for o in view.owners] == [{"id": "u1", "username": "alice"}]
    assert [(g.user.username, g.level) for g in view.grants] == [("bob", "read")]
    assert view.your_level == "owner" and view.public is False and view.rev == 0


async def test_grantee_reads_with_their_level(client):
    r = await client.get("/v1/gadgets/g1/access", headers=_as("u2"))
    assert r.status_code == 200 and r.json()["your_level"] == "read"


async def test_stranger_gets_404(client):
    r = await client.get("/v1/gadgets/g1/access", headers=_as("u3"))
    assert r.status_code == 404
    missing = await client.get("/v1/gadgets/nope/access", headers=_as("u3"))
    assert missing.status_code == 404


async def test_reader_cannot_change_sharing(client):
    r = await client.put("/v1/gadgets/g1/access", headers=_as("u2"), json={"owners": ["u2"]})
    assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied"


async def test_owner_shares_and_makes_public(client):
    r = await client.put(
        "/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "grants": {"u3": "full"}, "public": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["public"] is True and body["rev"] == 1
    assert [(g["user"]["id"], g["level"]) for g in body["grants"]] == [("u3", "full")]
    assert (await client.get("/v1/gadgets/g1/access", headers=_as("u3"))).json()["your_level"] == "full"
    # bob lost his grant but the row is public now: read
    assert (await client.get("/v1/gadgets/g1/access", headers=_as("u2"))).json()["your_level"] == "read"


async def test_put_access_refuses_empty_owners(client):
    r = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": []})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ["nobody", "ur", "ud"])
async def test_put_refuses_unknown_restricted_or_disabled_users(client, bad):
    as_owner = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1", bad]})
    as_grant = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "grants": {bad: "read"}})
    assert as_owner.status_code == 422 and as_grant.status_code == 422


async def test_owner_hands_over_ownership_and_keeps_only_a_grant(client):
    r = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u2"], "grants": {"u1": "read"}})
    assert r.status_code == 200, r.text
    again = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"]})
    assert again.status_code == 403


async def test_public_tools_refused_where_not_allowed(client):
    r = await client.put("/v1/plains/p1/access", headers=_as("u1"), json={"owners": ["u1"], "public_tools": ["t"]})
    assert r.status_code == 422
    ok = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "public_tools": ["t"]})
    assert ok.status_code == 200 and ok.json()["public_tools"] == ["t"]


async def test_stale_rev_conflicts(client):
    first = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "rev": 0})
    assert first.status_code == 200 and first.json()["rev"] == 1
    stale = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "rev": 0})
    assert stale.status_code == 409


async def test_admin_manages_any_row(client):
    view = await client.get("/v1/gadgets/g1/access", headers=_as("ua", "admin"))
    assert view.json()["your_level"] == "owner"
    r = await client.put("/v1/gadgets/g1/access", headers=_as("ua", "admin"), json={"owners": ["u3"]})
    assert r.status_code == 200 and [o["id"] for o in r.json()["owners"]] == ["u3"]


async def test_access_is_not_settable_with_unknown_fields(client):
    r = await client.put("/v1/gadgets/g1/access", headers=_as("u1"), json={"owners": ["u1"], "members": ["public"]})
    assert r.status_code == 422


async def test_lookup_matches_username_prefix_case_insensitively(client):
    r = await client.get("/v1/users/lookup", params={"q": "AL"}, headers=_as("u2"))
    assert r.status_code == 200
    # alba is restricted and albert is disabled: never offered
    assert sorted(u["username"] for u in r.json()) == ["Alfred", "alice"]
    assert set(r.json()[0]) == {"id", "username"}


async def test_lookup_empty_query_and_limit(client):
    assert (await client.get("/v1/users/lookup", params={"q": ""}, headers=_as("u2"))).json() == []
    r = await client.get("/v1/users/lookup", params={"q": "a", "limit": 1}, headers=_as("u2"))
    assert len(r.json()) == 1


async def test_lookup_treats_like_metacharacters_literally(client):
    r = await client.get("/v1/users/lookup", params={"q": "a_%"}, headers=_as("u2"))
    assert [u["username"] for u in r.json()] == ["a_%x"]


async def test_lookup_refuses_restricted_callers(client):
    r = await client.get("/v1/users/lookup", params={"q": "a"}, headers=_as("ur", "restricted"))
    assert r.status_code == 403
