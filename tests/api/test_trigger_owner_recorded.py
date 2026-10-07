"""The triggers REST routes record who saved each trigger and subscription (security review A-20).

A fired run is ranked by its owner (``primer.trigger.owner``), so every create and every update stamps ``owner`` from the
request's resolved actor (``request.state.actor``). The owner is server-set: a body cannot choose it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from primer.auth.passwords import hash_password
from primer.model.trigger import Subscription, Trigger
from primer.model.user import User
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


async def _register_admin(client) -> None:
    reg = await client.post("/v1/auth/register", json={"username": "ownadmin", "password": "ownadminpass1"})
    assert reg.status_code == 200, reg.text


async def _login(client, username: str, password: str) -> None:
    login = await client.post("/v1/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text


async def _seed_plain_user(app) -> None:
    await app.state.storage_provider.get_storage(User).create(User(
        id="user-own", username="ownuser", password_hash=await hash_password("ownuserpass1"),
        created_at=datetime.now(timezone.utc), role="user",
    ))


def _trigger_body() -> dict:
    fire_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {"slug": "owned", "name": "Owned", "config": {"kind": "delayed", "fire_at": fire_at}}


SUB_BODY = {"config": {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}}


async def _admin_id(app) -> str:
    users = app.state.storage_provider.get_storage(User)
    return next(u.id for u in users._data.values() if u.username == "ownadmin")  # noqa: SLF001


async def test_create_records_the_caller_as_owner_and_update_refreshes_it(client, app) -> None:
    await _register_admin(client)
    await _seed_plain_user(app)
    await _login(client, "ownuser", "ownuserpass1")

    created = await client.post("/v1/triggers", json=_trigger_body())
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    sub = await client.post(f"/v1/triggers/{tid}/subscriptions", json=SUB_BODY)
    assert sub.status_code == 201, sub.text
    sid = sub.json()["id"]

    sp = app.state.storage_provider
    trigger_row = await sp.get_storage(Trigger).get(tid)
    sub_row = await sp.get_storage(Subscription).get(sid)
    for row in (trigger_row, sub_row):
        assert row.owner is not None
        assert (row.owner.type, row.owner.id, row.owner.role) == ("user", "user-own", "user")
    assert created.json()["owner"]["id"] == "user-own"

    await _login(client, "ownadmin", "ownadminpass1")
    admin_id = await _admin_id(app)
    upd = await client.put(f"/v1/triggers/{tid}", json={"name": "Owned by admin now"})
    assert upd.status_code == 200, upd.text
    upd_sub = await client.put(f"/v1/triggers/{tid}/subscriptions/{sid}", json={"description": "re-saved"})
    assert upd_sub.status_code == 200, upd_sub.text

    trigger_row = await sp.get_storage(Trigger).get(tid)
    sub_row = await sp.get_storage(Subscription).get(sid)
    for row in (trigger_row, sub_row):
        assert (row.owner.type, row.owner.id, row.owner.role) == ("user", admin_id, "admin")


async def test_a_body_cannot_choose_the_owner(client, app) -> None:
    await _register_admin(client)
    await _seed_plain_user(app)
    await _login(client, "ownuser", "ownuserpass1")
    forged = {"type": "user", "id": "someone-else", "display": "x", "role": "admin", "source": "local"}

    created = await client.post("/v1/triggers", json={**_trigger_body(), "owner": forged})
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    sub = await client.post(f"/v1/triggers/{tid}/subscriptions", json={**SUB_BODY, "owner": forged})
    assert sub.status_code == 201, sub.text

    sp = app.state.storage_provider
    assert (await sp.get_storage(Trigger).get(tid)).owner.id == "user-own"
    assert (await sp.get_storage(Subscription).get(sub.json()["id"])).owner.id == "user-own"
