"""Triggers are owned and shareable; subscriptions inherit their trigger (access-control plan P3 Task 4, Decisions 3, 4, 8).

Levels (Decision 3): read sees a trigger and its subscriptions; full edits, fires now and manages subscriptions; owner
deletes, rotates the webhook token and shares. Out of access answers 404 (``trigger_not_found``, the missing-row answer), too
low a level 403 ``access_denied``. Run-as is unchanged: whoever saves a trigger becomes its run-as owner (``Trigger.owner``),
so a full grantee who edits acts as themselves. The minimal app: the error handlers, a test-only middleware turning an
``X-Test-User: <id>:<role>`` header into ``request.state.actor``, a real SQLite provider and the trigger routers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request

from primer.access.person import Person
from primer.access.service import AccessUpdate, set_access
from primer.api.errors import register_error_handlers
from primer.api.routers.triggers import trigger_access_router, triggers_router
from primer.bus.in_memory import InMemoryEventBus
from primer.model.access import Access
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.trigger import AgentFreshSubConfig, Subscription, Trigger, WebhookTriggerConfig
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.reach import refuse_fire_out_of_reach
from primer.trigger.row_writes import write_trigger_fields

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
TID, SID, TOKEN = "tr-1", "sb-1", "a" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ref(uid: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role="admin" if uid == ROOT else "user", source="local", user_id=uid)


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


@pytest_asyncio.fixture
async def sp(tmp_path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
        await provider.get_storage(User).create(
            User(id=uid, username=uid, created_at=NOW, role="admin" if uid == ROOT else "user")
        )
    # The subscriptions' target workspace: every user here holds full on it, so the trigger's own access is what is tested.
    await provider.get_storage(Workspace).create(Workspace(
        id="ws-1", template_id="tpl", provider_id="p", created_at=NOW, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://ws-1", token="t"),
        access=Access(owners=[ALICE], grants={BOB: "full", CAROL: "full", DAVE: "full"}),
    ))
    # tr-1: alice owns it, bob reads it, carol has full access, dave has nothing.
    await provider.get_storage(Trigger).create(Trigger(
        id=TID, slug="nightly", name="nightly", created_at=NOW, owner=_ref(ALICE),
        config=WebhookTriggerConfig(token=TOKEN),
        access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
    ))
    await provider.get_storage(Subscription).create(Subscription(
        id=SID, trigger_id=TID, created_at=NOW, owner=_ref(ALICE),
        config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1"),
    ))
    yield provider
    await provider.aclose()


@pytest_asyncio.fixture
async def client(sp) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()
    app.state.event_bus = InMemoryEventBus()

    @app.middleware("http")
    async def _actor(request: Request, call_next):
        uid, role = request.headers["X-Test-User"].split(":")
        request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
        return await call_next(request)

    app.include_router(trigger_access_router, prefix="/v1")
    app.include_router(triggers_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


_SUB_BODY = {"config": {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}}
_READ: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", f"/v1/triggers/{TID}", {}),
    ("GET", f"/v1/triggers/{TID}/access", {}),
    ("GET", f"/v1/triggers/{TID}/subscriptions", {}),
    ("GET", f"/v1/triggers/{TID}/subscriptions/{SID}", {}),
]
_FULL: list[tuple[str, str, dict[str, Any]]] = [
    ("PUT", f"/v1/triggers/{TID}", {"json": {"name": "renamed"}}),
    ("POST", f"/v1/triggers/{TID}/fire_now", {}),
    ("POST", f"/v1/triggers/{TID}/subscriptions", {"json": _SUB_BODY}),
    ("PUT", f"/v1/triggers/{TID}/subscriptions/{SID}", {"json": {"description": "d"}}),
    ("DELETE", f"/v1/triggers/{TID}/subscriptions/{SID}", {}),
]
_OWNER: list[tuple[str, str, dict[str, Any]]] = [
    ("DELETE", f"/v1/triggers/{TID}", {}),
    ("POST", f"/v1/triggers/{TID}/rotate_token", {}),
    ("PUT", f"/v1/triggers/{TID}/access", {"json": {"owners": [ALICE], "public": True}}),
]


def _ids(doors) -> list[str]:
    return [f"{m} {u.split(TID, 1)[1] or '/'}" for m, u, _ in doors]


def _code(r: httpx.Response) -> str | None:
    ext = r.json().get("extensions")
    return ext.get("code") if isinstance(ext, dict) else None


@pytest.mark.parametrize(("method", "url", "kw"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_a_private_trigger_is_invisible_to_a_stranger(client, method, url, kw):
    r = await client.request(method, url, headers=_as(DAVE), **kw)
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(("method", "url", "kw"), _READ, ids=_ids(_READ))
async def test_a_reader_reads(client, method, url, kw):
    r = await client.request(method, url, headers=_as(BOB), **kw)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(("method", "url", "kw"), _FULL + _OWNER, ids=_ids(_FULL + _OWNER))
async def test_a_reader_cannot_write(client, method, url, kw):
    r = await client.request(method, url, headers=_as(BOB), **kw)
    assert r.status_code == 403 and _code(r) == "access_denied", r.text


@pytest.mark.parametrize(("method", "url", "kw"), _READ + _FULL, ids=_ids(_READ + _FULL))
async def test_a_full_grantee_edits_fires_and_manages_subscriptions(client, method, url, kw):
    r = await client.request(method, url, headers=_as(CAROL), **kw)
    assert r.status_code < 400, r.text


@pytest.mark.parametrize(("method", "url", "kw"), _OWNER, ids=_ids(_OWNER))
async def test_a_full_grantee_cannot_delete_rotate_or_share(client, method, url, kw):
    r = await client.request(method, url, headers=_as(CAROL), **kw)
    assert r.status_code == 403 and _code(r) == "access_denied", r.text


@pytest.mark.parametrize("uid", [ALICE, ROOT])
@pytest.mark.parametrize(("method", "url", "kw"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_the_owner_and_an_admin_pass_every_door(client, method, url, kw, uid):
    r = await client.request(method, url, headers=_as(uid), **kw)
    assert r.status_code < 400, r.text


async def test_the_list_filters_in_storage(client, sp):
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-pub", slug="public-one", name="p", created_at=NOW, config=WebhookTriggerConfig(token="b" * 32),
        access=Access(owners=[ALICE], public=True),
    ))
    ids = lambda r: sorted(t["id"] for t in r.json()["items"])  # noqa: E731
    assert ids(await client.get("/v1/triggers", headers=_as(DAVE))) == ["tr-pub"]
    assert ids(await client.get("/v1/triggers", headers=_as(BOB))) == [TID, "tr-pub"]
    assert ids(await client.get("/v1/triggers", headers=_as(ROOT))) == [TID, "tr-pub"]
    assert ids(await client.get("/v1/triggers?kind=webhook", headers=_as(DAVE))) == ["tr-pub"]


async def test_a_reader_sees_the_token_masked_and_the_owner_reduced(client):
    body = (await client.get(f"/v1/triggers/{TID}", headers=_as(BOB))).json()
    assert body["config"]["token"] != TOKEN and body["owner"] == {"display": ALICE, "role": "user"}
    assert (await client.get(f"/v1/triggers/{TID}", headers=_as(ALICE))).json()["config"]["token"] == TOKEN


async def test_a_full_grantee_who_edits_becomes_the_run_as_owner_and_access_is_kept(client, sp):
    r = await client.put(f"/v1/triggers/{TID}", headers=_as(CAROL), json={"name": "renamed"})
    assert r.status_code == 200, r.text
    row = await sp.get_storage(Trigger).get(TID)
    assert row.owner.id == CAROL and row.name == "renamed"
    # The token is re-minted when the run-as owner changes: whoever knew the old one does not ride along.
    assert row.config.token != TOKEN
    assert row.access.owners == [ALICE] and row.access.grants == {BOB: "read", CAROL: "full"}


async def test_a_body_cannot_set_access(client, sp):
    r = await client.post(
        "/v1/triggers", headers=_as(DAVE),
        json={"slug": "mine", "name": "m", "config": {"kind": "webhook"}, "access": {"owners": [ALICE], "public": True}},
    )
    assert r.status_code == 201, r.text
    created = await sp.get_storage(Trigger).get(r.json()["id"])
    assert created.access.owners == [DAVE] and created.access.public is False
    r = await client.put(f"/v1/triggers/{TID}", headers=_as(CAROL), json={"name": "x", "access": {"owners": [CAROL]}})
    assert r.status_code == 200, r.text
    assert (await sp.get_storage(Trigger).get(TID)).access.owners == [ALICE]


async def test_the_owner_shares_and_the_grantee_then_reads(client):
    r = await client.put(f"/v1/triggers/{TID}/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {DAVE: "read"}})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/v1/triggers/{TID}/subscriptions", headers=_as(DAVE))).status_code == 200
    # bob lost his read grant with that write.
    assert (await client.get(f"/v1/triggers/{TID}", headers=_as(BOB))).status_code == 404


async def test_a_revoke_racing_a_fire_row_write_survives(sp):
    storage = sp.get_storage(Trigger)
    snapshot = await storage.get(TID)  # the fire read the row ...
    await set_access(storage, sp.get_storage(User), TID, Person(ALICE, "user"), AccessUpdate(owners=[ALICE]), what=TID)
    await write_trigger_fields(storage, snapshot, {"last_fired_id": "f-1", "last_fired_at": NOW})  # ... and writes after the revoke
    row = await storage.get(TID)
    assert row.last_fired_id == "f-1" and row.access.grants == {} and row.access.rev == 1


async def test_an_ownerless_subscription_never_fires(sp):
    """Migration 24 gives every legacy subscription its trigger's owner; one still without an owner is refused (fail closed)."""
    sub = (await sp.get_storage(Subscription).get(SID)).model_copy(update={"owner": None})
    assert await refuse_fire_out_of_reach(sub, sp) is not None
