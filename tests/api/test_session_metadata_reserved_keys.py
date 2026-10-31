"""Session-create ``metadata`` may not set the keys the runtime trusts (security review GS-1).

``PUT /v1/workspaces/{id}/reply_binding`` refuses a channel the caller cannot read, so ``POST /v1/workspaces/{id}/sessions`` must not
take the same binding (or any other system key: the trigger skip gate's ``subscription_id``, the relay flag, the one-turn schema) in
its ``metadata``. Ordinary caller keys still round-trip.
"""
from __future__ import annotations

import pytest

from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.workspace import Workspace
from primer.model.workspace_session import WorkspaceSession
from tests.api.test_session_access_routes import WS, _workspace, cast  # noqa: F401
from tests.api.test_session_create_attribution import _FakeWorkspaceRegistry

PRIVATE = "ch-private"
AGENT = {"kind": "agent", "agent_id": "operator"}
# Every key the runtime reads off a session's metadata and only an internal writer sets.
RESERVED = [
    "reply_binding", "relay_every_turn", "subscription_id", "trigger_id", "fire_id", "fired_at",
    "ephemeral_response_format", "invocation", "graph_input",
]


async def _seed(app, cast) -> None:  # noqa: F811
    app.state.workspace_registry = _FakeWorkspaceRegistry()
    sp = app.state.storage_provider
    # dave holds full on WS; the channel is carol's alone (dave holds no level on it).
    await sp.get_storage(Channel).create(
        Channel(id=PRIVATE, provider_id="cp-1", provider="slack", external_id="C1", access=Access(owners=[cast.carol_id])),
    )
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full"}))


async def _session_count(app) -> int:
    from primer.model.storage import OffsetPage

    page = await app.state.storage_provider.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=100))
    return page.total


async def test_a_forged_reply_binding_is_refused_like_the_guarded_door(app, client, cast):  # noqa: F811
    await _seed(app, cast)
    door = await cast.dave.put(f"/v1/workspaces/{WS}/reply_binding", json={"channel_id": PRIVATE})
    assert door.status_code == 422  # the guarded door refuses

    created = await cast.dave.post(f"/v1/workspaces/{WS}/sessions", json={
        "binding": AGENT,
        "metadata": {"reply_binding": {"channel_id": PRIVATE, "anchor": "1700000000.000100"}},
    })
    assert created.status_code == 422, created.text
    assert "reply_binding" in created.text
    assert await _session_count(app) == 0


@pytest.mark.parametrize("key", RESERVED)
async def test_every_reserved_key_is_refused(app, client, cast, key):  # noqa: F811
    await _seed(app, cast)
    created = await cast.dave.post(f"/v1/workspaces/{WS}/sessions", json={"binding": AGENT, "metadata": {key: "x"}})
    assert created.status_code == 422, created.text
    assert key in created.text


async def test_ordinary_metadata_still_round_trips(app, client, cast):  # noqa: F811
    await _seed(app, cast)
    md = {"ticket": "INC-1", "source": {"kind": "ui", "tags": ["a"]}}
    created = await cast.dave.post(f"/v1/workspaces/{WS}/sessions", json={"binding": AGENT, "metadata": md})
    assert created.status_code == 201, created.text
    assert created.json()["metadata"] == md
    row = await app.state.storage_provider.get_storage(WorkspaceSession).get(created.json()["id"])
    assert row.metadata == md
