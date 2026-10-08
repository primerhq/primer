"""A webhook trigger's token is served and changed only by its owner or an admin (lead review of #491, round 2).

The token is the webhook's only credential, and a webhook POST renders its body into the first message of every run the
trigger's subscriptions start, at the owners' rank. Before this, any ``role=user`` account could read an admin's token from
``GET /v1/triggers`` (or mint a fresh one with ``rotate_token``) and inject a payload into an admin-ranked run. Now:

* list / get show everyone else the mask;
* ``rotate_token`` and ``PUT`` on a webhook trigger answer 403 ``forbidden_role`` unless the caller owns it or is an admin, and
  the caller becomes the owner;
* a ``PUT`` that hands a webhook trigger to a different owner mints a new token, so nobody who knew the old one rides along;
* the mask sent back in a ``PUT`` body keeps the stored token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.auth.passwords import hash_password
from primer.model.trigger import Trigger
from primer.model.user import User
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

MASK = "•••redacted•••"


async def _register_admin(client) -> None:
    reg = await client.post("/v1/auth/register", json={"username": "tokadmin", "password": "tokadminpass1"})
    assert reg.status_code == 200, reg.text


async def _login(client, username: str, password: str) -> None:
    login = await client.post("/v1/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text


async def _as_admin(client) -> None:
    await _login(client, "tokadmin", "tokadminpass1")


async def _as_user(client) -> None:
    await _login(client, "tokuser", "tokuserpass1")


async def _setup(client, app) -> None:
    await _register_admin(client)
    await app.state.storage_provider.get_storage(User).create(User(
        id="user-tok", username="tokuser", password_hash=await hash_password("tokuserpass1"),
        created_at=datetime.now(timezone.utc), role="user",
    ))


async def _create_webhook(client, slug: str) -> tuple[str, str]:
    created = await client.post("/v1/triggers", json={"slug": slug, "name": slug, "config": {"kind": "webhook"}})
    assert created.status_code == 201, created.text
    body = created.json()
    assert len(body["config"]["token"]) == 32
    return body["id"], body["config"]["token"]


def _code(resp) -> str | None:
    ext = resp.json().get("extensions")
    return ext.get("code") if isinstance(ext, dict) else None


async def _stored(app, tid: str) -> Trigger:
    return await app.state.storage_provider.get_storage(Trigger).get(tid)


async def test_a_user_sees_the_mask_on_an_admin_webhook_trigger_and_the_owner_and_an_admin_see_the_token(client, app):
    await _setup(client, app)
    await _as_admin(client)
    tid, token = await _create_webhook(client, "admin-hook")

    assert (await client.get(f"/v1/triggers/{tid}")).json()["config"]["token"] == token
    listed = (await client.get("/v1/triggers")).json()["items"]
    assert [t["config"]["token"] for t in listed if t["id"] == tid] == [token]

    await _as_user(client)
    got = await client.get(f"/v1/triggers/{tid}")
    assert got.status_code == 200, got.text
    assert got.json()["config"]["token"] == MASK
    listed = (await client.get("/v1/triggers")).json()["items"]
    assert [t["config"]["token"] for t in listed if t["id"] == tid] == [MASK]


async def test_a_user_sees_the_token_of_their_own_webhook_trigger(client, app):
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")

    assert (await client.get(f"/v1/triggers/{tid}")).json()["config"]["token"] == token


async def test_a_user_cannot_rotate_an_admin_webhook_token(client, app):
    await _setup(client, app)
    await _as_admin(client)
    tid, token = await _create_webhook(client, "admin-hook")
    before = await _stored(app, tid)

    await _as_user(client)
    resp = await client.post(f"/v1/triggers/{tid}/rotate_token")

    assert resp.status_code == 403, resp.text
    assert _code(resp) == "forbidden_role"
    after = await _stored(app, tid)
    assert after.config.token == token
    assert after.owner == before.owner


async def test_a_user_cannot_put_an_admin_webhook_trigger(client, app):
    await _setup(client, app)
    await _as_admin(client)
    tid, token = await _create_webhook(client, "admin-hook")
    before = await _stored(app, tid)

    await _as_user(client)
    renamed = await client.put(f"/v1/triggers/{tid}", json={"name": "mine"})
    chosen = await client.put(f"/v1/triggers/{tid}", json={"config": {"kind": "webhook", "token": "b" * 32}})

    for resp in (renamed, chosen):
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "forbidden_role"
        assert token not in resp.text
    after = await _stored(app, tid)
    assert (after.name, after.config.token, after.owner) == (before.name, token, before.owner)


async def test_the_owner_rotates_and_stays_owner(client, app):
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")

    resp = await client.post(f"/v1/triggers/{tid}/rotate_token")

    assert resp.status_code == 200, resp.text
    new = resp.json()["config"]["token"]
    assert new != token and len(new) == 32
    assert (await _stored(app, tid)).owner.id == "user-tok"


async def test_an_admin_who_rotates_a_user_trigger_becomes_its_owner(client, app):
    await _setup(client, app)
    await _as_user(client)
    tid, _ = await _create_webhook(client, "user-hook")

    await _as_admin(client)
    resp = await client.post(f"/v1/triggers/{tid}/rotate_token")

    assert resp.status_code == 200, resp.text
    stored = await _stored(app, tid)
    assert stored.owner.id != "user-tok" and stored.owner.role == "admin"


async def test_an_admin_who_re_saves_a_user_webhook_trigger_gets_a_new_token(client, app):
    """Adopting someone else's webhook trigger must not carry along everyone who knew its token."""
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")

    await _as_admin(client)
    resp = await client.put(f"/v1/triggers/{tid}", json={"name": "adopted"})

    assert resp.status_code == 200, resp.text
    stored = await _stored(app, tid)
    assert stored.owner.role == "admin"
    assert stored.config.token != token and len(stored.config.token) == 32
    assert resp.json()["config"]["token"] == stored.config.token


async def test_the_owner_re_saving_keeps_the_token_and_the_mask_in_a_body_keeps_it_too(client, app):
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")

    renamed = await client.put(f"/v1/triggers/{tid}", json={"name": "renamed"})
    masked = await client.put(f"/v1/triggers/{tid}", json={"config": {"kind": "webhook", "token": MASK}})

    assert renamed.status_code == 200 and masked.status_code == 200, (renamed.text, masked.text)
    assert (await _stored(app, tid)).config.token == token


# ---------------------------------------------------------------------------
# Lead review of #491, round 3
# ---------------------------------------------------------------------------


async def test_an_admin_put_that_echoes_the_stored_token_still_gets_a_new_token(client, app):
    """GET then a modified PUT carries the token it read. A change of owner must still re-mint it: the old owner knows it."""
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")

    await _as_admin(client)
    read = (await client.get(f"/v1/triggers/{tid}")).json()
    assert read["config"]["token"] == token
    resp = await client.put(f"/v1/triggers/{tid}", json={"name": "adopted", "config": read["config"]})

    assert resp.status_code == 200, resp.text
    stored = await _stored(app, tid)
    assert stored.owner.role == "admin"
    assert stored.config.token != token and len(stored.config.token) == 32


async def test_the_hmac_secret_mask_in_a_put_body_keeps_the_stored_secret(client, app):
    await _setup(client, app)
    await _as_user(client)
    tid, token = await _create_webhook(client, "user-hook")
    set_secret = await client.put(f"/v1/triggers/{tid}", json={"config": {"kind": "webhook", "hmac_secret": "s3cret-value"}})
    assert set_secret.status_code == 200, set_secret.text

    read = (await client.get(f"/v1/triggers/{tid}")).json()
    assert read["config"]["hmac_secret"] == "**********"
    resp = await client.put(f"/v1/triggers/{tid}", json={"config": read["config"]})

    assert resp.status_code == 200, resp.text
    stored = await _stored(app, tid)
    assert stored.config.hmac_secret.get_secret_value() == "s3cret-value"
    assert stored.config.token == token


async def test_a_caller_who_cannot_manage_a_trigger_sees_only_the_owner_display_and_role(client, app):
    await _setup(client, app)
    await _as_admin(client)
    tid, _ = await _create_webhook(client, "admin-hook")
    sub = await client.post(
        f"/v1/triggers/{tid}/subscriptions",
        json={"config": {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}},
    )
    assert sub.status_code == 201, sub.text
    sid = sub.json()["id"]
    full = (await client.get(f"/v1/triggers/{tid}")).json()["owner"]
    assert {"type", "id", "display", "role"} <= set(full)

    await _as_user(client)
    rows = [
        (await client.get(f"/v1/triggers/{tid}")).json(),
        next(t for t in (await client.get("/v1/triggers")).json()["items"] if t["id"] == tid),
        (await client.get(f"/v1/triggers/{tid}/subscriptions/{sid}")).json(),
        next(s for s in (await client.get(f"/v1/triggers/{tid}/subscriptions")).json()["items"] if s["id"] == sid),
    ]
    for row in rows:
        assert row["owner"] == {"display": full["display"], "role": "admin"}, row
