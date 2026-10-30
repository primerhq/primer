"""make_crud_router(owned=True): levels on get/update/delete, visibility on list/find (access-control spec 6.2, 9; plan Task 5).

The test-only actor middleware turns the X-Test-User header into
``request.state.actor`` the way AuthMiddleware does for the cookie and
bearer paths (Task 2 pins those), so every request here is decided by
the same ``person_from_request`` / ``require`` / ``visible_predicate``
the production routes call. Rows are seeded the way Task 6's access
sub-resource will change them: a direct storage write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from starlette.datastructures import State

from primer.api.errors import register_error_handlers
from primer.api.routers._crud import make_crud_router
from primer.model.access import Access, Owned
from primer.model.common import Identifiable
from primer.model.principal import Principal
from tests.conftest import _FakeStorageProvider


class _Widget(Identifiable, Owned):
    """A tiny owned row for the router tests."""

    name: str = ""


def _header(headers, name: str) -> str | None:
    for k, v in headers:
        if k.lower() == name.encode():
            return v.decode()
    return None


def _actor_for(username: str | None) -> Principal | None:
    if username is None:
        return None
    if username == "system":
        return Principal(type="system", id="system", display="system", role=None, source="internal")
    role = "admin" if username == "admin" else "user"
    return Principal(type="user", id=username, display=username, role=role, source="local", user_id=username)


class _ActorMiddleware:
    """Test-only stand-in for AuthMiddleware: X-Test-User -> scope.state.actor."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            state = scope.get("state")
            if not isinstance(state, State):
                state = State()
                scope["state"] = state
            state.actor = _actor_for(_header(scope.get("headers", ()), "x-test-user"))
        await self.app(scope, receive, send)


def _make_app(provider: _FakeStorageProvider) -> FastAPI:
    app = FastAPI()

    async def _widget_storage():
        return provider.get_storage(_Widget)

    app.add_middleware(_ActorMiddleware)
    app.include_router(
        make_crud_router(
            model_cls=_Widget,
            storage_dep=_widget_storage,
            plural="widgets",
            tag="widgets",
            owned=True,
            search_fields=["name"],
        )
    )
    register_error_handlers(app)
    return app


@pytest.fixture
def provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.fixture
async def client(provider: _FakeStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    app = _make_app(provider)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _headers(user: str | None) -> dict[str, str]:
    return {} if user is None else {"X-Test-User": user}


async def _grant(provider: _FakeStorageProvider, widget_id: str, access: Access) -> None:
    """Set a row's access directly, the way Task 6's access sub-resource will."""
    storage = provider.get_storage(_Widget)
    row = await storage.get(widget_id)
    row.access = access
    await storage.update(row)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_stamps_caller_as_owner(client, provider):
    # The body's access block is ignored: the caller is stamped as owner.
    r = await client.post(
        "/v1/widgets",
        json={"id": "w1", "name": "mine", "access": {"owners": ["u2"], "public": True}},
        headers=_headers("u1"),
    )
    assert r.status_code == 201, r.text
    # The response row keeps access: callers need it to show sharing.
    assert r.json()["access"]["owners"] == ["u1"]
    assert r.json()["access"]["public"] is False
    row = await provider.get_storage(_Widget).get("w1")
    assert row.access.owners == ["u1"]


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_unshared_is_404(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "u1 only"}, headers=_headers("u1"))
    r = await client.get("/v1/widgets/w1", headers=_headers("u2"))
    assert r.status_code == 404
    body = r.json()
    assert body["type"] == "/errors/not-found"
    # The 404 text is the missing-row text for this id: no information leak.
    assert body["detail"] == "Widget 'w1' does not exist"


async def test_get_shared_read_ok(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "shared"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], grants={"u2": "read"}))
    r = await client.get("/v1/widgets/w1", headers=_headers("u2"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "shared"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_with_read_is_403(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "original"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], grants={"u2": "read"}))
    r = await client.put("/v1/widgets/w1", json={"id": "w1", "name": "sneaky"}, headers=_headers("u2"))
    assert r.status_code == 403
    body = r.json()
    assert body["type"] == "/errors/forbidden"
    assert body["extensions"]["code"] == "access_denied"
    assert body["detail"] == "Widget 'w1' needs full access"
    row = await provider.get_storage(_Widget).get("w1")
    assert row.name == "original", "a refused update writes nothing"


async def test_update_with_full_ok(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "original"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], grants={"u2": "full"}))
    r = await client.put("/v1/widgets/w1", json={"id": "w1", "name": "edited"}, headers=_headers("u2"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "edited"


async def test_update_ignores_access_in_body(client, provider):
    # Review focus 2: a PUT carrying an access block leaves the stored
    # access unchanged.
    await client.post("/v1/widgets", json={"id": "w1", "name": "x"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], grants={"u2": "full"}))
    r = await client.put(
        "/v1/widgets/w1",
        json={"id": "w1", "name": "y", "access": {"owners": ["u2"], "public": True}},
        headers=_headers("u2"),
    )
    assert r.status_code == 200, r.text
    row = await provider.get_storage(_Widget).get("w1")
    assert row.access.owners == ["u1"]
    assert row.access.grants == {"u2": "full"}
    assert row.access.public is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_needs_owner(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "x"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], grants={"u2": "full"}))
    r = await client.delete("/v1/widgets/w1", headers=_headers("u2"))
    assert r.status_code == 403
    assert r.json()["extensions"]["code"] == "access_denied"
    assert r.json()["detail"] == "Widget 'w1' needs owner access"
    assert await provider.get_storage(_Widget).get("w1") is not None
    r = await client.delete("/v1/widgets/w1", headers=_headers("u1"))
    assert r.status_code == 204
    assert await provider.get_storage(_Widget).get("w1") is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_shows_only_visible(client, provider):
    for wid in ("w-private", "w-shared", "w-public"):
        r = await client.post("/v1/widgets", json={"id": wid, "name": wid}, headers=_headers("u1"))
        assert r.status_code == 201, r.text
    await _grant(provider, "w-shared", Access(owners=["u1"], grants={"u2": "read"}))
    await _grant(provider, "w-public", Access(owners=["u1"], public=True))

    async def _ids(user: str | None) -> list[str]:
        r = await client.get("/v1/widgets", headers=_headers(user))
        assert r.status_code == 200, r.text
        return sorted(item["id"] for item in r.json()["items"])

    assert await _ids("u1") == ["w-private", "w-public", "w-shared"]
    assert await _ids("u2") == ["w-public", "w-shared"]
    assert await _ids("u3") == ["w-public"]
    assert await _ids(None) == [], "an unauthenticated request sees nothing"


async def test_list_q_still_filters_visible(client, provider):
    # q narrows within the caller's visible set, never widens it.
    await client.post("/v1/widgets", json={"id": "w-secret", "name": "secret-thing"}, headers=_headers("u1"))
    await client.post("/v1/widgets", json={"id": "w-shared", "name": "shared-thing"}, headers=_headers("u1"))
    await client.post("/v1/widgets", json={"id": "w-public", "name": "public-thing"}, headers=_headers("u1"))
    await _grant(provider, "w-shared", Access(owners=["u1"], grants={"u2": "read"}))
    await _grant(provider, "w-public", Access(owners=["u1"], public=True))

    async def _ids(user: str, q: str) -> list[str]:
        r = await client.get("/v1/widgets", params={"q": q}, headers=_headers(user))
        assert r.status_code == 200, r.text
        return sorted(item["id"] for item in r.json()["items"])

    assert await _ids("u2", "thing") == ["w-public", "w-shared"]
    assert await _ids("u2", "secret") == []
    assert await _ids("u1", "secret") == ["w-secret"]


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def _find_body(predicate: dict | None) -> dict:
    return {"predicate": predicate, "page": {"kind": "offset", "offset": 0, "length": 100}}


async def test_find_ands_access_predicate(client, provider):
    await client.post("/v1/widgets", json={"id": "w-private", "name": "hidden"}, headers=_headers("u1"))
    await client.post("/v1/widgets", json={"id": "w-shared", "name": "shown"}, headers=_headers("u1"))
    await _grant(provider, "w-shared", Access(owners=["u1"], grants={"u2": "read"}))

    # Review focus 3: a body predicate naming the PRIVATE row must not
    # surface it for u2: the access predicate is ANDed, not replaced.
    r = await client.post(
        "/v1/widgets/find",
        json=_find_body(
            {"left": {"name": "name"}, "op": "~=*", "right": {"value": "%hidden%"}}
        ),
        headers=_headers("u2"),
    )
    assert r.status_code == 200, r.text
    assert [item["id"] for item in r.json()["items"]] == []

    # The access field itself is not a caller predicate: matching u1's
    # membership token must not widen u2's results.
    r = await client.post(
        "/v1/widgets/find",
        json=_find_body(
            {"left": {"name": "access.members"}, "op": "contains", "right": {"value": "u:u1"}}
        ),
        headers=_headers("u2"),
    )
    assert r.status_code == 200, r.text
    assert [item["id"] for item in r.json()["items"]] == []

    # For the owner the body predicate still works as before.
    r = await client.post(
        "/v1/widgets/find",
        json=_find_body(
            {"left": {"name": "access.members"}, "op": "contains", "right": {"value": "u:u1"}}
        ),
        headers=_headers("u1"),
    )
    assert r.status_code == 200, r.text
    assert sorted(item["id"] for item in r.json()["items"]) == ["w-private", "w-shared"]


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------


async def test_admin_sees_all(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "a"}, headers=_headers("u1"))
    r = await client.get("/v1/widgets/w1", headers=_headers("admin"))
    assert r.status_code == 200, r.text
    listing = await client.get("/v1/widgets", headers=_headers("admin"))
    assert [item["id"] for item in listing.json()["items"]] == ["w1"]
    # An admin-created row is admin-managed: no owner, nothing public.
    r = await client.post("/v1/widgets", json={"id": "w-admin", "name": "b"}, headers=_headers("admin"))
    assert r.status_code == 201, r.text
    assert r.json()["access"]["owners"] == []
    assert r.json()["access"]["public"] is False


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------


async def test_public_visible_to_all_users(client, provider):
    await client.post("/v1/widgets", json={"id": "w1", "name": "open"}, headers=_headers("u1"))
    await _grant(provider, "w1", Access(owners=["u1"], public=True))
    for user in ("u2", "u3", "u9"):
        r = await client.get("/v1/widgets/w1", headers=_headers(user))
        assert r.status_code == 200, f"{user} should read the public row: {r.text}"
    listing = await client.get("/v1/widgets", headers=_headers("u9"))
    assert [item["id"] for item in listing.json()["items"]] == ["w1"]
