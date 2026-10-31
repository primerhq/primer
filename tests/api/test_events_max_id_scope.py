"""A non-admin's ``max_id`` does not count events of workspaces they cannot read (final review F13)."""
from __future__ import annotations

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.conftest import fake_storage_provider  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user, _seed_workspace


async def test_max_id_is_scoped_to_the_named_workspace_for_a_non_admin(client, app):  # noqa: F811
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    await _seed_workspace(app, "ws-alice", alice)
    store = app.state.storage_provider.get_event_store()
    mine = await store.append(event_type="session.started", workspace_id="ws-alice")
    for _ in range(3):  # another workspace's events, newer than alice's
        await store.append(event_type="session.started", workspace_id="ws-other")
    head = await store.max_id()
    assert head > mine

    await _login(client, "alice")
    r = await client.get("/v1/events", params={"workspace_id": "ws-alice"})
    assert r.status_code == 200, r.text
    assert r.json()["max_id"] == mine != head

    admin_view = None
    await _login_admin(client)
    admin_view = (await client.get("/v1/events")).json()
    assert admin_view["max_id"] == head


async def _login_admin(client):
    login = await client.post("/v1/auth/login", json={"username": "chanadmin", "password": "chanadminpassword1"})
    assert login.status_code == 200, login.text
