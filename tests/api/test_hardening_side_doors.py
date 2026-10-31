"""The two REST side doors the access review found (ACL-P1-HARD-A, security review findings 1 and 3).

GET /v1/toolsets/{id}/oauth/status read a toolset row (its sign-in state) without asking the caller for read access,
and PUT /v1/workspaces/{id}/reply_binding named a channel without asking the caller to be able to read it. A row the
caller holds no level on now answers exactly like a missing one (spec 6.2, 9): the same 404 problem for the toolset,
the same 422 ``channel_not_found`` for the channel.

Each exploit runs three ways: user B (a full user holding no level) is refused like a missing id, B may act once the
owner shares read, and an admin always may.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import SecretStr

from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.provider import Toolset
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from tests.api.test_mcp_oauth_routes import _login_as, _store, oauth_app  # noqa: F401
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import make_toolset

TS = "kb"
OWNER = "owner-a"
WID = "w-bind"
CHANNEL = "ch-1"


async def _seed_private_toolset(app, toolset_id: str = TS) -> Toolset:
    """An MCP toolset owned by ``owner-a``: the seeded ``u-user`` holds no level on it."""
    row = make_toolset("http://mcp.example", oauth={}, toolset_id=toolset_id)
    row.access = Access(owners=[OWNER])
    await _store(app, row)
    return row


# ===========================================================================
# Finding 1: GET /v1/toolsets/{id}/oauth/status needs read on the toolset
# ===========================================================================


async def test_a_user_without_read_cannot_read_the_sign_in_status(raw_client, oauth_app):
    await _seed_private_toolset(oauth_app)
    await _login_as(raw_client, oauth_app, "user")
    refused = await raw_client.get(f"/v1/toolsets/{TS}/oauth/status")
    missing = await raw_client.get("/v1/toolsets/no-such/oauth/status")
    assert refused.status_code == 404
    assert missing.status_code == 404
    # Unreadable answers exactly like a missing id: same problem type, no existence told.
    assert refused.json()["type"] == missing.json()["type"] == "/errors/not-found"


async def test_a_user_reads_the_sign_in_status_once_the_owner_shares_read(raw_client, oauth_app):
    row = await _seed_private_toolset(oauth_app)
    await _login_as(raw_client, oauth_app, "user")
    row.access = Access(owners=[OWNER], grants={"u-user": "read"})
    await oauth_app.state.storage_provider.get_storage(Toolset).update(row)
    ok = await raw_client.get(f"/v1/toolsets/{TS}/oauth/status")
    assert ok.status_code == 200, ok.text
    assert ok.json()["toolset_id"] == TS


async def test_admin_reads_the_sign_in_status(raw_client, oauth_app):
    await _seed_private_toolset(oauth_app)
    await _login_as(raw_client, oauth_app, "admin")
    ok = await raw_client.get(f"/v1/toolsets/{TS}/oauth/status")
    assert ok.status_code == 200, ok.text


# ===========================================================================
# Finding 3: PUT /v1/workspaces/{id}/reply_binding needs read on the named channel
# ===========================================================================


def _workspace() -> Workspace:
    return Workspace(
        id=WID,
        template_id="tpl-1",
        provider_id="wp-1",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://localhost:5959", token=SecretStr("tok")),
    )


async def _seed_channel_world(app) -> None:
    """A running workspace owned by ``u-b`` (binding needs a full write on it) and a channel owned by ``owner-a``: the seeded
    users hold no level on the channel."""
    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(_workspace().model_copy(update={"access": Access(owners=["u-b"])}))
    await sp.get_storage(Channel).create(
        Channel(id=CHANNEL, provider_id="cp-1", provider="slack", external_id="C1", access=Access(owners=[OWNER])),
    )


async def test_a_user_cannot_bind_a_workspace_to_a_channel_he_cannot_read(app, raw_client):
    await _seed_channel_world(app)
    await _seed(app, uid="u-b", username="userb", role="user")
    await _login(raw_client, "userb")
    refused = await raw_client.put(f"/v1/workspaces/{WID}/reply_binding", json={"channel_id": CHANNEL})
    assert refused.status_code == 422, refused.text
    # The answer is the missing-channel answer, to the letter.
    body = refused.json()
    assert body["extensions"]["error"] == "channel_not_found"
    assert body["extensions"]["channel_id"] == CHANNEL
    assert body["extensions"]["message"] == f"Channel {CHANNEL!r} does not exist."
    # Nothing was written.
    row = await app.state.storage_provider.get_storage(Workspace).get(WID)
    assert row.reply_binding is None


async def test_a_user_binds_once_the_owner_shares_read(app, raw_client):
    await _seed_channel_world(app)
    await _seed(app, uid="u-b", username="userb", role="user")
    await _login(raw_client, "userb")
    channel = await app.state.storage_provider.get_storage(Channel).get(CHANNEL)
    channel.access = Access(owners=[OWNER], grants={"u-b": "read"})
    await app.state.storage_provider.get_storage(Channel).update(channel)
    ok = await raw_client.put(f"/v1/workspaces/{WID}/reply_binding", json={"channel_id": CHANNEL})
    assert ok.status_code == 200, ok.text
    row = await app.state.storage_provider.get_storage(Workspace).get(WID)
    assert row.reply_binding is not None and row.reply_binding.channel_id == CHANNEL


async def test_admin_binds_a_workspace_to_a_channel(app, raw_client):
    await _seed_channel_world(app)
    await _seed(app, uid="u-admin", username="adminb", role="admin")
    await _login(raw_client, "adminb")
    ok = await raw_client.put(f"/v1/workspaces/{WID}/reply_binding", json={"channel_id": CHANNEL})
    assert ok.status_code == 200, ok.text
