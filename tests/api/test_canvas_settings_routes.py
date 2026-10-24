"""``GET`` and ``PUT /v1/canvas_settings`` (pivot P5, Task 15; Decision 14): the admin's canvas settings, one row, replaced whole."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings

DEFAULTS = CanvasSettings(id=CANVAS_SETTINGS_ID).model_dump(mode="json")


@pytest_asyncio.fixture
async def carol(app, client) -> AsyncIterator[httpx.AsyncClient]:
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        yield c


async def test_the_defaults_until_an_admin_saves(client, fake_storage_provider):
    r = await client.get("/v1/canvas_settings")
    assert r.status_code == 200 and r.json() == DEFAULTS
    assert await fake_storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None, "reading stores nothing"


async def test_put_replaces_the_row(client, fake_storage_provider):
    r = await client.put("/v1/canvas_settings", json={"media_policy": "allowlist", "media_allowlist": ["Media.Example.com"], "max_events": 50})
    assert r.status_code == 200, r.text
    assert (r.json()["media_policy"], r.json()["media_allowlist"], r.json()["max_events"]) == ("allowlist", ["media.example.com"], 50)
    assert (await client.get("/v1/canvas_settings")).json() == r.json()
    r = await client.put("/v1/canvas_settings", json={"strict_mode": True, "id": "somewhere-else"})
    assert r.status_code == 200
    assert r.json() == {**DEFAULTS, "strict_mode": True}, "a replace: the fields left out take their defaults"
    assert r.json()["id"] == CANVAS_SETTINGS_ID
    stored = await fake_storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID)
    assert stored.strict_mode is True and stored.media_policy == "any-https"


@pytest.mark.parametrize("body", [
    {"max_elements": 0},
    {"media_policy": "everything"},
    {"media_allowlist": ["not a host"]},
    {"max_waking_events_per_minute": 1000},
])
async def test_an_invalid_body_is_422_and_stores_nothing(client, fake_storage_provider, body):
    r = await client.put("/v1/canvas_settings", json=body)
    assert r.status_code == 422, r.text
    assert await fake_storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None


async def test_settings_are_admin_only(client, carol, fake_storage_provider):
    for r in (await carol.get("/v1/canvas_settings"), await carol.put("/v1/canvas_settings", json={"strict_mode": True})):
        assert r.status_code == 403 and r.json()["extensions"]["error"] == "forbidden_role"
    assert await fake_storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None
    assert (await client.get("/v1/canvas_settings")).status_code == 200


async def test_the_canvas_read_carries_the_effective_settings(client, fake_storage_provider):
    from datetime import UTC, datetime

    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.WAITING, created_at=datetime.now(UTC),
    ))
    await client.put("/v1/canvas_settings", json={"media_policy": "none", "strict_mode": True})
    body = (await client.get("/v1/workspaces/ws-1/sessions/s1/canvas")).json()
    assert body["settings"] == {"media_policy": "none", "media_allowlist": [], "strict_mode": True}
