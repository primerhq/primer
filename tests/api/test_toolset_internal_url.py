"""``POST /v1/toolsets`` and the create-time probe of an MCP server at an internal address (review of workspaces, W25).

A literal internal URL is reserved to an admin (403 ``forbidden_role``, the text says why); a NAME that resolves internal is refused by the probe
for a caller below admin (it connects through the egress guard); an admin keeps every option, ``PRIMER_EGRESS_ALLOW`` included.
"""

from __future__ import annotations

import pytest

from primer.common import netguard
from tests.api.test_require_user_admin import _login, _seed

pytestmark = pytest.mark.asyncio


def _body(url: str, toolset_id: str = "ts-int") -> dict:
    return {"id": toolset_id, "provider": "mcp", "config": {"transport": "http", "config": {"url": url}}}


async def _as(raw_client, app, role: str) -> None:
    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)


@pytest.fixture(autouse=True)
def _no_allowlist():
    netguard.configure_egress_allow([])
    yield
    netguard.configure_egress_allow([])


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest", "http://10.0.0.5:5432/mcp", "http://127.0.0.1:1/mcp", "http://[::1]:1/mcp"])
async def test_a_plain_user_cannot_point_a_toolset_at_an_internal_address(raw_client, app, url):
    await _as(raw_client, app, "user")

    resp = await raw_client.post("/v1/toolsets", json=_body(url), params={"allow_unreachable": "true"})

    assert resp.status_code == 403, resp.text
    assert "internal address" in resp.json()["detail"] and "admin" in resp.json()["detail"]
    assert (await raw_client.get("/v1/toolsets/ts-int")).status_code == 404, "the refused toolset was stored"


async def test_an_admin_may_configure_an_internal_server(raw_client, app):
    await _as(raw_client, app, "admin")

    resp = await raw_client.post("/v1/toolsets", json=_body("http://127.0.0.1:1/mcp"), params={"allow_unreachable": "true"})

    assert resp.status_code == 201, resp.text


async def test_the_probe_refuses_a_name_that_resolves_internal_for_a_plain_user(raw_client, app, monkeypatch):
    """The name looks public to the save-time check; the probe connects through the guard and the answer is a private address."""
    async def resolves_private(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr(netguard, "_resolve", resolves_private)
    await _as(raw_client, app, "user")

    resp = await raw_client.post("/v1/toolsets", json=_body("http://mcp.rebound.example/mcp"))

    assert resp.status_code == 403, resp.text
    assert "private address" in resp.json()["detail"]
    assert (await raw_client.get("/v1/toolsets/ts-int")).status_code == 404


async def test_the_probe_does_not_guard_an_admin(raw_client, app, monkeypatch):
    async def resolves_private(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(netguard, "_resolve", resolves_private)
    await _as(raw_client, app, "admin")

    resp = await raw_client.post("/v1/toolsets", json=_body("http://mcp.internal.example:1/mcp"))

    assert resp.status_code != 403, resp.text  # unreachable (nothing listens), but not refused as internal
