"""GET /v1/events + /v1/event_subscriptions over the fake app harness."""
from __future__ import annotations

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


OPA_EXPRS = [
    "package primer.event_filter\nmatch := opa.runtime() != {}\n",
    "package primer.event_filter\nmatch := opa.future_host_builtin()\n",
]
OPA_REFUSAL = (
    "event filter expr may use only the event document through input; "
    "references to the opa package are not allowed"
)


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
@pytest.mark.parametrize("expr", OPA_EXPRS, ids=["runtime", "any-opa-package"])
async def test_subscription_create_refuses_opa_package_references(client, app, expr) -> None:
    await _login(client)

    refused = await client.post(
        "/v1/event_subscriptions",
        json={
            "description": "unsafe log tap",
            "filter": {"event_types": ["agent.*"], "expr": expr},
            "sink": {"kind": "log"},
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == OPA_REFUSAL
    listed = await client.get("/v1/event_subscriptions")
    assert all(row["description"] != "unsafe log tap" for row in listed.json()["items"])


TEMPLATE_EXPRS = [
    'package primer.event_filter\nmatch := $"{opa.runtime().env.HOME}" == "/root"\n',
    'package primer.event_filter\nmatch := $`{opa.runtime().env.HOME}` == "/root"\n',
]


@pytest.mark.asyncio
@pytest.mark.parametrize("expr", TEMPLATE_EXPRS, ids=["double-quoted", "raw"])
async def test_subscription_create_refuses_template_strings(client, app, expr) -> None:
    """G6-2: the code inside a template string's {...} parts is inside the quotes the opa check blanks."""
    await _login(client)

    refused = await client.post(
        "/v1/event_subscriptions",
        json={
            "description": "template tap",
            "filter": {"event_types": ["agent.*"], "expr": expr},
            "sink": {"kind": "log"},
        },
    )

    assert refused.status_code == 422, refused.text
    assert "template strings" in refused.json()["detail"]
    listed = await client.get("/v1/event_subscriptions")
    assert all(row["description"] != "template tap" for row in listed.json()["items"])


@pytest.mark.asyncio
async def test_subscription_update_refuses_opa_package_references(client, app) -> None:
    await _login(client)
    safe_expr = (
        "package primer.event_filter\n"
        'match := input.payload.collection_id == "kb"\n'
    )
    created = await client.post(
        "/v1/event_subscriptions",
        json={
            "description": "safe log tap",
            "filter": {"event_types": ["agent.*"], "expr": safe_expr},
            "sink": {"kind": "log"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    body["filter"]["expr"] = OPA_EXPRS[0]

    refused = await client.put(
        f"/v1/event_subscriptions/{body['id']}", json=body,
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == OPA_REFUSAL
    stored = await client.get(f"/v1/event_subscriptions/{body['id']}")
    assert stored.status_code == 200, stored.text
    assert stored.json()["filter"]["expr"] == safe_expr


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


@pytest.mark.asyncio
async def test_workspace_terminal_access_toggle(client, app) -> None:
    """PUT /v1/workspaces/{id}/terminal_access grants/revokes non-admin
    access to the workspace's integrated terminal (defaults to False)."""
    await _login(client)
    from datetime import datetime, timezone

    from pydantic import SecretStr

    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-term-1", description="terminal access toggle probe",
        template_id="tpl-1", provider_id="p-1",
        created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:1/", token=SecretStr("t"),
        ),
    ))

    # Off by default.
    row = await sp.get_storage(Workspace).get("ws-term-1")
    assert row.terminal_user_access is False

    on = await client.put(
        "/v1/workspaces/ws-term-1/terminal_access", json={"enabled": True},
    )
    assert on.status_code == 200, on.text
    assert on.json()["terminal_user_access"] is True

    off = await client.put(
        "/v1/workspaces/ws-term-1/terminal_access", json={"enabled": False},
    )
    assert off.status_code == 200, off.text
    assert off.json()["terminal_user_access"] is False

    missing = await client.put(
        "/v1/workspaces/nope/terminal_access", json={"enabled": True},
    )
    assert missing.status_code == 404, missing.text
