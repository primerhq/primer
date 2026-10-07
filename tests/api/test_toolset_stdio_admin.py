"""Creating or changing an MCP ``stdio`` toolset requires an admin (architecture review A-02).

A stdio toolset names a command that the primer process launches on the server host when the toolset is probed or called.
Provider configuration (LLM, embedding, SSO, web search, ...) is admin-only because it is system configuration; this is the same
class of power, but toolsets sit on the user tier ("an authoring feature"), so any non-restricted user could run any command (RUN:
``POST /v1/toolsets`` with ``/bin/sh -c id`` answered 201). The REST route now refuses a create or update that involves a stdio
toolset (on either side of an update) unless the caller is an admin; every other toolset stays user-tier, and deleting stays as it
was. The system ``create_toolset`` / ``update_toolset`` tools are pinned in ``tests/toolset/test_system_toolset_stdio_admin.py``.
"""

from __future__ import annotations

import pytest

from tests.api.test_require_user_admin import _login, _seed

STDIO = {"id": "ts-stdio", "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["/bin/echo", "hi"]}}}
STDIO_OTHER_COMMAND = {**STDIO, "config": {"transport": "stdio", "config": {"command": ["/bin/sh", "-c", "id"]}}}
HTTP = {"id": "ts-http", "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp"}}}
NO_PROBE = {"allow_unreachable": "true"}


async def _as(raw_client, app, role: str):
    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)


async def _admin_creates(raw_client, app, body: dict, **params):
    await _as(raw_client, app, "admin")
    resp = await raw_client.post("/v1/toolsets", json=body, params=params)
    assert resp.status_code == 201, resp.text
    raw_client.cookies.clear()


async def test_a_plain_user_cannot_create_a_stdio_toolset(raw_client, app):
    await _as(raw_client, app, "user")

    resp = await raw_client.post("/v1/toolsets", json=STDIO)

    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    detail = resp.json()["detail"]
    assert "stdio" in detail and "admin" in detail, f"the console shows this text, so it must say why: {detail!r}"
    assert (await raw_client.get("/v1/toolsets/ts-stdio")).status_code == 404, "the refused toolset was stored"


async def test_a_plain_user_cannot_change_an_existing_stdio_toolset(raw_client, app):
    await _admin_creates(raw_client, app, STDIO)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-stdio", json=STDIO_OTHER_COMMAND)

    assert resp.status_code == 403, resp.text
    stored = (await raw_client.get("/v1/toolsets/ts-stdio")).json()
    assert stored["config"]["config"]["command"] == ["/bin/echo", "hi"], "a refused update changed the command"


async def test_a_plain_user_cannot_turn_an_http_toolset_into_a_stdio_one(raw_client, app):
    await _admin_creates(raw_client, app, HTTP, **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-http", json={**STDIO, "id": "ts-http"})

    assert resp.status_code == 403, resp.text
    assert (await raw_client.get("/v1/toolsets/ts-http")).json()["config"]["transport"] == "http"


async def test_an_admin_can_create_and_change_a_stdio_toolset(raw_client, app):
    await _as(raw_client, app, "admin")

    created = await raw_client.post("/v1/toolsets", json=STDIO)
    changed = await raw_client.put("/v1/toolsets/ts-stdio", json=STDIO_OTHER_COMMAND)

    assert (created.status_code, changed.status_code) == (201, 200), (created.text, changed.text)


async def test_a_plain_user_still_manages_toolsets_that_launch_nothing(raw_client, app):
    await _as(raw_client, app, "user")

    created = await raw_client.post("/v1/toolsets", json=HTTP, params=NO_PROBE)
    changed = await raw_client.put(
        "/v1/toolsets/ts-http", json={**HTTP, "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/other"}}},
        params=NO_PROBE,
    )

    assert (created.status_code, changed.status_code) == (201, 200), (created.text, changed.text)


async def test_a_plain_user_can_still_delete_a_stdio_toolset(raw_client, app):
    """Deleting launches nothing; the gate is on creating and changing the command (the lead's scope for A-02)."""
    await _admin_creates(raw_client, app, STDIO)
    await _as(raw_client, app, "user")

    assert (await raw_client.delete("/v1/toolsets/ts-stdio")).status_code == 204
