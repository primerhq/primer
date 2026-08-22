"""GET /v1/events + /v1/event_subscriptions over the fake app harness."""
from __future__ import annotations

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


async def _login(client) -> None:
    reg = await client.post(
        "/v1/auth/register",
        json={"username": "evadmin", "password": "evadminpass1"},
    )
    assert reg.status_code == 200, reg.text


@pytest.mark.asyncio
async def test_events_window_pagination_and_glob(client, app) -> None:
    await _login(client)
    store = app.state.storage_provider.get_event_store()
    base = await store.max_id()
    a = await store.append(event_type="agent.created", entity_kind="agent",
                           entity_id="a1")
    await store.append(event_type="graph.created", entity_kind="graph",
                       entity_id="g1")
    b = await store.append(event_type="agent.deleted", entity_kind="agent",
                           entity_id="a1")

    resp = await client.get(f"/v1/events?after_id={base}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["event_type"] for e in body["items"]] == [
        "agent.created", "graph.created", "agent.deleted",
    ]
    assert body["max_id"] == b

    globbed = await client.get(f"/v1/events?after_id={base}&event_type=agent.*")
    assert [e["id"] for e in globbed.json()["items"]] == [a, b]

    scoped = await client.get(
        f"/v1/events?after_id={base}&entity_kind=agent&entity_id=a1&limit=1"
    )
    assert len(scoped.json()["items"]) == 1


@pytest.mark.asyncio
async def test_subscription_crud_and_guards(client, app) -> None:
    await _login(client)

    created = await client.post(
        "/v1/event_subscriptions",
        json={
            "description": "my log tap",
            "filter": {"event_types": ["agent.*"]},
            "sink": {"kind": "log"},
        },
    )
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]

    # converge sinks are system-only.
    refused = await client.post(
        "/v1/event_subscriptions",
        json={"description": "x", "sink": {"kind": "converge"}},
    )
    assert refused.status_code == 422, refused.text

    # The paused toggle works on any row.
    paused = await client.post(
        f"/v1/event_subscriptions/{sub_id}/paused", json={"paused": True},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True

    listed = await client.get("/v1/event_subscriptions")
    assert any(r["id"] == sub_id for r in listed.json()["items"])

    gone = await client.delete(f"/v1/event_subscriptions/{sub_id}")
    assert gone.status_code == 204, gone.text


@pytest.mark.asyncio
async def test_system_rows_reject_update_delete_but_pause(client, app) -> None:
    await _login(client)
    from primer.bootstrap.seed import ensure_system_event_subscriptions

    await ensure_system_event_subscriptions(app.state.storage_provider)

    upd = await client.put(
        "/v1/event_subscriptions/system-logger",
        json={
            "id": "system-logger", "description": "hijack",
            "sink": {"kind": "log"}, "managed_by": None,
        },
    )
    assert upd.status_code == 409, upd.text
    dele = await client.delete("/v1/event_subscriptions/system-logger")
    assert dele.status_code == 409, dele.text

    paused = await client.post(
        "/v1/event_subscriptions/system-logger/paused",
        json={"paused": True},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True


@pytest.mark.asyncio
async def test_workspace_events_toggle(client, app) -> None:
    """PUT /v1/workspaces/{id}/events opts a workspace in and out."""
    await _login(client)
    from datetime import datetime, timezone

    from pydantic import SecretStr

    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-evt-1", description="events toggle probe",
        template_id="tpl-1", provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:1/", token=SecretStr("t"),
        ),
    ))

    on = await client.put(
        "/v1/workspaces/ws-evt-1/events",
        json={"config": {"enabled": True, "kinds": ["file_changed"]}},
    )
    assert on.status_code == 200, on.text
    assert on.json()["events"]["enabled"] is True
    assert on.json()["events"]["kinds"] == ["file_changed"]

    off = await client.put(
        "/v1/workspaces/ws-evt-1/events", json={"config": None},
    )
    assert off.status_code == 200, off.text
    assert off.json()["events"] is None

    missing = await client.put(
        "/v1/workspaces/nope/events", json={"config": None},
    )
    assert missing.status_code == 404, missing.text
