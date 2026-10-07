"""A python toolset is admin-only, and a caller below admin cannot repoint a toolset while its stored secrets ride along (security
sweep AUTHZ-01, SSRF-01, SSRF-02, SEC-02).

A python toolset's source runs on the server host (LocalHardenedRunner), so ``POST`` / ``PUT /v1/toolsets`` of one is refused below
admin with 403 ``forbidden_role``, on either side of an update, exactly as for a stdio MCP toolset
(``tests/api/test_toolset_stdio_admin.py``). A ``PUT`` that changes a toolset's URL or OAuth endpoints while sending a secret back as
the served mask would carry the stored headers / client secret to the new endpoint, so below admin it is refused until the secrets
are re-entered. The system tools are pinned in ``tests/toolset/test_system_toolset_write_privilege.py``.
"""

from __future__ import annotations

from primer.model.provider import Toolset
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.test_system_toolset_write_privilege import CLIENT_SECRET, PY_SOURCE, SECRET, _http

PYTHON = {"id": "ts-py", "provider": "python", "config": {"source": PY_SOURCE, "source_version": 1}}
PYTHON_OTHER = {**PYTHON, "config": {"source": PY_SOURCE.replace("hello", "hi"), "source_version": 1}}
HTTP_PLAIN = {"id": "ts-py", "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp"}}}
NO_PROBE = {"allow_unreachable": "true"}


async def _as(raw_client, app, role: str):
    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)


async def _admin_creates(raw_client, app, body: dict, **params):
    await _as(raw_client, app, "admin")
    resp = await raw_client.post("/v1/toolsets", json=body, params=params)
    assert resp.status_code == 201, resp.text
    raw_client.cookies.clear()


# ---- python toolsets (AUTHZ-01, SSRF-01) ----------------------------------------------------------------------------------------


async def test_a_plain_user_cannot_create_a_python_toolset(raw_client, app):
    await _as(raw_client, app, "user")

    resp = await raw_client.post("/v1/toolsets", json=PYTHON)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "forbidden_role", resp.text
    detail = resp.json()["detail"]
    assert "python" in detail and "admin" in detail, f"the console shows this text, so it must say why: {detail!r}"
    assert (await raw_client.get("/v1/toolsets/ts-py")).status_code == 404, "the refused toolset was stored"


async def test_a_plain_user_cannot_change_a_python_toolset(raw_client, app):
    await _admin_creates(raw_client, app, PYTHON)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-py", json=PYTHON_OTHER)

    assert resp.status_code == 403, resp.text
    assert "hello" in (await raw_client.get("/v1/toolsets/ts-py")).json()["config"]["source"]


async def test_a_plain_user_cannot_turn_an_http_toolset_into_a_python_one(raw_client, app):
    await _admin_creates(raw_client, app, {**HTTP_PLAIN, "id": "ts-x"}, **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-x", json={**PYTHON, "id": "ts-x"})

    assert resp.status_code == 403, resp.text
    assert (await raw_client.get("/v1/toolsets/ts-x")).json()["provider"] == "mcp"


async def test_a_plain_user_cannot_turn_a_python_toolset_into_an_http_one(raw_client, app):
    await _admin_creates(raw_client, app, PYTHON)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-py", json=HTTP_PLAIN, params=NO_PROBE)

    assert resp.status_code == 403, resp.text
    assert (await raw_client.get("/v1/toolsets/ts-py")).json()["provider"] == "python"


async def test_an_admin_can_create_and_change_a_python_toolset(raw_client, app):
    await _as(raw_client, app, "admin")

    created = await raw_client.post("/v1/toolsets", json=PYTHON)
    changed = await raw_client.put("/v1/toolsets/ts-py", json=PYTHON_OTHER)

    assert (created.status_code, changed.status_code) == (201, 200), (created.text, changed.text)


# ---- repointing a toolset that holds secrets (SSRF-02, SEC-02) ------------------------------------------------------------------


async def _served_with_url(raw_client, toolset_id: str, url: str) -> dict:
    """The row as GET serves it (secrets masked), with only the URL changed: what a client that edits one field sends back."""
    body = (await raw_client.get(f"/v1/toolsets/{toolset_id}")).json()
    body["config"]["config"]["url"] = url
    return body


async def test_a_plain_user_cannot_repoint_a_toolset_while_its_headers_ride_along_masked(raw_client, app):
    await _admin_creates(raw_client, app, _http("http://127.0.0.1:9/mcp", SECRET), **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put(
        "/v1/toolsets/ts-http", json=await _served_with_url(raw_client, "ts-http", "http://evil.example/mcp"), params=NO_PROBE,
    )

    assert resp.status_code == 403, resp.text
    assert "re-enter" in resp.json()["detail"], resp.text
    assert (await raw_client.get("/v1/toolsets/ts-http")).json()["config"]["config"]["url"] == "http://127.0.0.1:9/mcp"


async def test_a_plain_user_cannot_repoint_a_toolset_while_its_oauth_client_secret_rides_along(raw_client, app):
    await _admin_creates(raw_client, app, _http("http://127.0.0.1:9/mcp", SECRET, client_secret=CLIENT_SECRET), **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put(
        "/v1/toolsets/ts-http", json=await _served_with_url(raw_client, "ts-http", "http://evil.example/mcp"), params=NO_PROBE,
    )

    assert resp.status_code == 403, resp.text
    assert (await raw_client.get("/v1/toolsets/ts-http")).json()["config"]["config"]["url"] == "http://127.0.0.1:9/mcp"


async def test_a_plain_user_may_repoint_a_toolset_when_it_re_enters_the_secrets(raw_client, app):
    await _admin_creates(raw_client, app, _http("http://127.0.0.1:9/mcp", SECRET), **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put("/v1/toolsets/ts-http", json=_http("http://other.example/mcp", "Bearer new"), params=NO_PROBE)

    assert resp.status_code == 200, resp.text
    assert (await raw_client.get("/v1/toolsets/ts-http")).json()["config"]["config"]["url"] == "http://other.example/mcp"


async def test_a_plain_user_keeps_masked_secrets_when_the_endpoint_does_not_change(raw_client, app):
    await _admin_creates(raw_client, app, _http("http://127.0.0.1:9/mcp", SECRET), **NO_PROBE)
    await _as(raw_client, app, "user")

    resp = await raw_client.put(
        "/v1/toolsets/ts-http", json=await _served_with_url(raw_client, "ts-http", "http://127.0.0.1:9/mcp"), params=NO_PROBE,
    )

    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(Toolset).get("ts-http")
    assert stored.config.config.headers["Authorization"].get_secret_value() == SECRET


async def test_an_admin_may_repoint_a_toolset_and_keep_its_masked_secrets(raw_client, app):
    await _admin_creates(raw_client, app, _http("http://127.0.0.1:9/mcp", SECRET), **NO_PROBE)
    await _login(raw_client, "admin")

    resp = await raw_client.put(
        "/v1/toolsets/ts-http", json=await _served_with_url(raw_client, "ts-http", "http://other.example/mcp"), params=NO_PROBE,
    )

    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(Toolset).get("ts-http")
    assert (stored.config.config.url, stored.config.config.headers["Authorization"].get_secret_value()) == (
        "http://other.example/mcp", SECRET,
    )
