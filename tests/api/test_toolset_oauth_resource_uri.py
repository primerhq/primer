"""``oauth.resource_uri`` must be on the MCP server's own origin (audit G4-07).

The resource indicator is what the authorization server mints the token for. A ``resource_uri`` on another origin would ask for a token meant for
another server and send it to this one. Every toolset write refuses it with a 422 whose error names the field: the REST create and update (the
``loc`` of a validation error, so the console shows it under the field), the system tools (``validation-error`` naming the field) and a harness
install (a refused bundle).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from primer.harness.service import apply_install
from primer.model.harness import Harness, RenderedEntry
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from tests.api.test_require_user_admin import _login, _seed

NO_PROBE = {"allow_unreachable": "true"}
LOC = ["body", "config", "config", "oauth", "resource_uri"]


def _body(resource_uri: str, url: str = "https://mcp.example/mcp") -> dict:
    return {
        "id": "kb",
        "provider": "mcp",
        "config": {"transport": "http", "config": {"url": url, "oauth": {"scopes": ["read"], "resource_uri": resource_uri}}},
    }


async def _admin(raw_client, app) -> None:
    await _seed(app, uid="u-admin", username="admin", role="admin")
    await _login(raw_client, "admin")


def _refused_at_the_field(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == "/errors/validation-error"
    [error] = body["extensions"]["errors"]
    assert error["loc"] == LOC
    assert "origin" in error["msg"] and "oauth.resource_uri" in error["msg"]


async def test_a_create_with_a_resource_uri_on_another_origin_is_refused_at_the_field(raw_client, app) -> None:
    await _admin(raw_client, app)
    for other in ("https://other.example/mcp", "http://mcp.example/mcp", "https://mcp.example:8443/mcp", "not a url"):
        _refused_at_the_field(await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body(other)))
    assert await app.state.storage_provider.get_storage(Toolset).get("kb") is None


async def test_a_resource_uri_on_the_servers_origin_is_kept(raw_client, app) -> None:
    await _admin(raw_client, app)
    for same, url in (("https://MCP.example:443/tenants/1", "https://mcp.example/mcp"), ("https://mcp.example/mcp", "https://user:pw@mcp.example/mcp")):
        response = await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body(same, url))
        assert response.status_code == 201, response.text
        assert (await app.state.storage_provider.get_storage(Toolset).get("kb")).config.config.oauth.resource_uri == same
        assert (await raw_client.delete("/v1/toolsets/kb")).status_code == 204


async def test_an_update_that_moves_the_resource_uri_to_another_origin_is_refused(raw_client, app) -> None:
    await _admin(raw_client, app)
    assert (await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body("https://mcp.example/mcp"))).status_code == 201
    _refused_at_the_field(await raw_client.put("/v1/toolsets/kb", params=NO_PROBE, json=_body("https://other.example/mcp")))
    moved_url = _body("https://mcp.example/mcp", url="https://elsewhere.example/mcp")
    _refused_at_the_field(await raw_client.put("/v1/toolsets/kb", params=NO_PROBE, json=moved_url))
    stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
    assert (str(stored.config.config.url), stored.config.config.oauth.resource_uri) == ("https://mcp.example/mcp", "https://mcp.example/mcp")


async def test_the_system_tools_refuse_it_naming_the_field(app) -> None:
    from primer.toolset.system import build_system_toolset
    from tests._support.caller import caller

    tools = build_system_toolset(
        storage_provider=app.state.storage_provider, provider_registry=app.state.provider_registry, mcp_oauth=app.state.mcp_oauth,
    )
    created = await tools.call(tool_name="create_toolset", arguments={"entity": _body("https://other.example/mcp")}, ctx=caller("admin"))
    assert created.is_error
    error = json.loads(created.output)
    assert error["type"] == "validation-error" and "config.config.oauth.resource_uri" in error["message"], created.output

    ok = await tools.call(tool_name="create_toolset", arguments={"entity": _body("https://mcp.example/r")}, ctx=caller("admin"))
    assert not ok.is_error, ok.output
    updated = await tools.call(tool_name="update_toolset", arguments={"id": "kb", "entity": _body("https://other.example/r")}, ctx=caller("admin"))
    assert updated.is_error and json.loads(updated.output)["type"] == "validation-error"


async def test_a_harness_install_refuses_it(app) -> None:
    admin = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
    harness = Harness(id="acme-id", slug="acme", name="Acme", git_url="https://github.com/x/y", operation_requested_by=admin,
                      created_at=datetime.now(UTC))
    payload = {key: value for key, value in _body("https://other.example/mcp").items() if key != "id"}
    entry = RenderedEntry(kind="toolset", template_name="ts", resolved_id="acme__kb", template_source_hash="h", rendered_hash="r1",
                          rendered_payload=payload)
    error = await apply_install(storage_provider=app.state.storage_provider, harness=harness, entries=[entry], rendered_files_by_name={},
                                bundle_hash="bh", overrides_hash="oh", schema_hash=None)
    assert error is not None and json.loads(error)["code"] == "invalid_resource_uri", error
    assert await app.state.storage_provider.get_storage(Toolset).get("acme__kb") is None
