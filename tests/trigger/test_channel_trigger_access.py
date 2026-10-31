"""A channel trigger needs READ on its channel (access-control Phase 3, Decision 5).

A channel trigger's events ARE channel content, so the person who saves the trigger - or binds a
subscription onto it - must hold ``read`` on the channel it fires from:

* a channel the person holds no level on is refused exactly like a missing one (404, missing-row
  text over REST, ``type=not-found`` through the tools - spec 9: no leak of "exists but not yours"),
* a provider-wide config (``channel_id`` null) listens to every channel the provider has and is
  admin-only (403, ``code: access_denied`` over REST, ``type=forbidden`` through the tools).

Covered on every save door: the REST create/update routes, the trigger tools (the crud toolset
re-exports the same handlers) and ``system__create_channel_binding``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr

from primer.auth.passwords import hash_password
from primer.model.access import Access
from primer.model.channel import Channel, ChannelProviderType
from primer.model.principal import PrincipalRef
from primer.model.trigger import Trigger
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.yield_ import ToolContext
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel_config(channel_id: str | None = "ch-alice") -> dict:
    cfg: dict = {"kind": "channel", "provider_id": "prov-1"}
    if channel_id is not None:
        cfg["channel_id"] = channel_id
    return cfg


def _delayed_body(slug: str = "delayed-1") -> dict:
    return {
        "slug": slug,
        "name": "Delayed",
        "config": {"kind": "delayed", "fire_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    }


async def _register_admin(client) -> None:
    """The first-boot operator account (registration locks after the first user; house pattern)."""
    reg = await client.post("/v1/auth/register", json={"username": "chanadmin", "password": "chanadminpassword1"})
    assert reg.status_code == 200, reg.text


async def _seed_user(app, username: str) -> str:
    """A plain user seeded straight into storage (house pattern: tests/api/test_trigger_owner_recorded.py)."""
    users = app.state.storage_provider.get_storage(User)
    uid = f"u-{username}"
    await users.create(User(
        id=uid, username=username,
        password_hash=await hash_password(f"{username}password1"),
        created_at=datetime.now(timezone.utc),
        role="user",
    ))
    return uid


async def _login(client, username: str) -> None:
    login = await client.post("/v1/auth/login", json={"username": username, "password": f"{username}password1"})
    assert login.status_code == 200, login.text


async def _seed_channel(app, owner: str, *, channel_id: str = "ch-alice", grants: dict | None = None, public: bool = False) -> None:
    await app.state.storage_provider.get_storage(Channel).create(Channel(
        id=channel_id,
        provider_id="prov-1",
        provider=ChannelProviderType.SLACK,
        external_id=f"ext-{channel_id}",
        access=Access(owners=[owner], grants=dict(grants or {}), public=public),
    ))


async def _seed_workspace(app, ws_id: str, owner: str) -> None:
    """The row ``workspace_level`` reads (house seed: tests/api/test_workspace_access_routes.py)."""
    await app.state.storage_provider.get_storage(Workspace).create(Workspace(
        id=ws_id,
        template_id="tpl-1",
        provider_id="local-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")),
        access=Access(owners=[owner]),
    ))


def _ctx(user_id: str, role: str = "user") -> ToolContext:
    return ToolContext(
        tool_call_id="tc-1", session_id=None, workspace_id=None,
        initiated_by=PrincipalRef(
            type="user", id=user_id, display=user_id, role=role, source="local",
        ),  # type: ignore[arg-type]
    )


def _refusal(result) -> dict:
    assert result.is_error, f"expected a refusal, got: {result.output}"
    return json.loads(result.output)


SUB_CONFIG = {"kind": "agent_fresh_session", "workspace_id": "ws-bob", "agent_id": "ag-1"}


# ---------------------------------------------------------------------------
# REST: create
# ---------------------------------------------------------------------------


async def test_a_user_cannot_create_a_trigger_bound_to_a_channel_they_cannot_read(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _login(client, "bob")
    await _seed_channel(app, alice)

    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
    )

    assert created.status_code == 404, created.text
    body = created.json()
    assert body["type"] == "/errors/not-found", body
    assert "does not exist" in body["detail"], body
    rows = (await app.state.storage_provider.get_storage(Trigger).list(OffsetPage(offset=0, length=50))).items
    assert rows == [], "a refused trigger stores nothing"


async def test_a_user_cannot_update_a_trigger_into_a_channel_they_cannot_read(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    mallory = await _seed_user(app, "mallory")
    await _seed_channel(app, alice)
    await _seed_channel(app, mallory, channel_id="ch-mallory")

    # Alice owns the trigger and the channel it names; she saves it.
    await _login(client, "alice")
    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    # ...but the same save cannot re-point it at a channel she cannot read.
    updated = await client.put(f"/v1/triggers/{tid}", json={"config": _channel_config("ch-mallory")})

    assert updated.status_code == 404, updated.text
    assert updated.json()["type"] == "/errors/not-found", updated.json()
    row = await app.state.storage_provider.get_storage(Trigger).get(tid)
    assert row.config.channel_id == "ch-alice", "a refused update changes nothing"


async def test_a_read_grant_lets_the_user_create_and_update(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _login(client, "bob")
    await _seed_channel(app, alice, grants={bob: "read"})

    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    updated = await client.put(f"/v1/triggers/{tid}", json={"config": _channel_config()})
    assert updated.status_code == 200, updated.text
    assert updated.json()["config"]["kind"] == "channel"


async def test_a_public_channel_is_saveable_by_any_user(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _login(client, "bob")
    await _seed_channel(app, alice, public=True)

    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
    )
    assert created.status_code == 201, created.text


# ---------------------------------------------------------------------------
# REST: provider-wide (channel_id null) is admin-only
# ---------------------------------------------------------------------------


async def test_a_non_admin_cannot_save_a_provider_wide_channel_trigger(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _login(client, "bob")
    await _seed_channel(app, alice, channel_id="ch-pub", public=True)

    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config(None)},
    )
    assert created.status_code == 403, created.text
    body = created.json()
    assert body["type"] == "/errors/forbidden", body
    assert body["extensions"]["code"] == "access_denied", body

    # A channel-kind trigger bob can save (public channel), re-pointed provider-wide: refused.
    bound = await client.post(
        "/v1/triggers",
        json={"slug": "chan-x", "name": "Chan", "config": _channel_config("ch-pub")},
    )
    assert bound.status_code == 201, bound.text
    updated = await client.put(f"/v1/triggers/{bound.json()['id']}", json={"config": _channel_config(None)})
    assert updated.status_code == 403, updated.text
    assert updated.json()["extensions"]["code"] == "access_denied", updated.json()


async def test_an_admin_can_save_a_provider_wide_and_a_bound_channel_trigger(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    await _seed_channel(app, alice)

    provider_wide = await client.post(
        "/v1/triggers",
        json={"slug": "chan-wide", "name": "Wide", "config": _channel_config(None)},
    )
    assert provider_wide.status_code == 201, provider_wide.text

    bound = await client.post(
        "/v1/triggers",
        json={"slug": "chan-bound", "name": "Bound", "config": _channel_config()},
    )
    assert bound.status_code == 201, bound.text


# ---------------------------------------------------------------------------
# Tools: the trigger toolset (crud re-exports these handlers)
# ---------------------------------------------------------------------------


async def test_the_create_tool_refuses_an_unreadable_channel_and_a_provider_wide(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _seed_channel(app, alice)
    tools = app.state.provider_registry._trigger_toolset_provider

    refused = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
        ctx=_ctx(bob),
    )
    body = _refusal(refused)
    assert body["type"] == "not-found", body
    assert "does not exist" in body["message"], body

    wide = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-x", "name": "Chan", "config": _channel_config(None)},
        ctx=_ctx(bob),
    )
    body = _refusal(wide)
    assert body["type"] == "forbidden", body


async def test_the_update_tool_refuses_an_unreadable_channel_and_a_provider_wide(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    mallory = await _seed_user(app, "mallory")
    await _seed_channel(app, alice)
    await _seed_channel(app, mallory, channel_id="ch-mallory")
    tools = app.state.provider_registry._trigger_toolset_provider

    created = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-x", "name": "Chan", "config": _channel_config()},
        ctx=_ctx(alice),
    )
    assert not created.is_error, created.output
    tid = json.loads(created.output)["id"]

    refused = await tools.call(
        tool_name="update",
        arguments={"id": tid, "config": _channel_config("ch-mallory")},
        ctx=_ctx(alice),
    )
    body = _refusal(refused)
    assert body["type"] == "not-found", body
    row = await app.state.storage_provider.get_storage(Trigger).get(tid)
    assert row.config.channel_id == "ch-alice", "a refused update changes nothing"

    wide = await tools.call(
        tool_name="update",
        arguments={"id": tid, "config": _channel_config(None)},
        ctx=_ctx(alice),
    )
    body = _refusal(wide)
    assert body["type"] == "forbidden", body


async def test_the_tools_allow_a_granted_or_public_channel_and_an_admin_provider_wide(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _seed_channel(app, alice, grants={bob: "read"})
    tools = app.state.provider_registry._trigger_toolset_provider

    granted = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-granted", "name": "Chan", "config": _channel_config()},
        ctx=_ctx(bob),
    )
    assert not granted.is_error, granted.output

    await app.state.storage_provider.get_storage(Channel).delete("ch-alice")
    await _seed_channel(app, alice, public=True)
    public_ = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-public", "name": "Chan", "config": _channel_config()},
        ctx=_ctx(bob),
    )
    assert not public_.is_error, public_.output

    admin = await tools.call(
        tool_name="create",
        arguments={"slug": "chan-wide", "name": "Chan", "config": _channel_config(None)},
        ctx=_ctx(alice, role="admin"),
    )
    assert not admin.is_error, admin.output


# ---------------------------------------------------------------------------
# Tools: system__create_channel_binding (the bind-time gate)
# ---------------------------------------------------------------------------


async def test_a_user_cannot_bind_a_subscription_to_a_channel_trigger_on_an_unreadable_channel(client, app) -> None:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    bob = await _seed_user(app, "bob")
    await _seed_channel(app, alice)

    # Alice owns the channel, so she can create the trigger bound to it.
    await _login(client, "alice")
    created = await client.post(
        "/v1/triggers",
        json={"slug": "chan-bound", "name": "Bound", "config": _channel_config()},
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    refused = await app.state.system_toolset.call(
        tool_name="create_channel_binding",
        arguments={"trigger_id": tid, "config": SUB_CONFIG},
        ctx=_ctx(bob),
    )
    body = _refusal(refused)
    assert body["type"] == "not-found", body
    # The refusal is the channel's missing-row words, not the subscription target's.
    assert "ch-alice" in body["message"], body

    # A read grant on the channel the trigger names opens the same bind...
    sp = app.state.storage_provider
    await sp.get_storage(Channel).delete("ch-alice")
    await _seed_channel(app, alice, grants={bob: "read"})
    # ...and the subscription target (bob's workspace) is one he holds full on.
    await _seed_workspace(app, "ws-bob", bob)

    allowed = await app.state.system_toolset.call(
        tool_name="create_channel_binding",
        arguments={"trigger_id": tid, "config": SUB_CONFIG},
        ctx=_ctx(bob),
    )
    assert not allowed.is_error, allowed.output
