"""POST /v1/toolsets connectivity probe: block creating an MCP toolset
whose http endpoint is unreachable, with a ``?allow_unreachable=true``
escape hatch ("Create anyway").

Contract under test (single source of truth shared with the Console):
an unreachable http MCP toolset is rejected BEFORE persistence with
HTTP 400 + problem ``type == "/errors/toolset-unreachable"``. The probe
is skipped for the bypass flag, for non-MCP toolsets, and for stdio MCP
(which has no remote endpoint).
"""

from __future__ import annotations

import pytest


# A closed loopback port: connecting refuses immediately (ECONNREFUSED),
# so the probe fails fast without leaning on the 8s timeout.
_DEAD_URL = "http://127.0.0.1:1/mcp"


def _http_mcp_body(tid: str, url: str = _DEAD_URL) -> dict:
    return {
        "id": tid,
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {"url": url, "headers": {}},
        },
    }


@pytest.mark.asyncio
async def test_unreachable_http_mcp_is_rejected_and_not_persisted(client):
    r = await client.post("/v1/toolsets", json=_http_mcp_body("ts-dead"))
    assert r.status_code == 400, r.text

    body = r.json()
    # Frontend-detectable contract: the dedicated problem type URI.
    assert body["type"] == "/errors/toolset-unreachable"
    assert body["status"] == 400
    assert "content-type" in {k.lower() for k in r.headers}
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "connect" in body["detail"].lower()

    # Nothing was persisted — the create was aborted before storage.create.
    g = await client.get("/v1/toolsets/ts-dead")
    assert g.status_code == 404, g.text


@pytest.mark.asyncio
async def test_allow_unreachable_bypasses_the_probe_and_creates(client):
    r = await client.post(
        "/v1/toolsets?allow_unreachable=true",
        json=_http_mcp_body("ts-anyway"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "ts-anyway"

    # Persisted despite being unreachable.
    g = await client.get("/v1/toolsets/ts-anyway")
    assert g.status_code == 200, g.text


@pytest.mark.asyncio
async def test_stdio_mcp_is_created_without_probing(client):
    # stdio has no remote endpoint to reach; the probe is skipped, so the
    # row is created even though the command would never launch.
    r = await client.post(
        "/v1/toolsets",
        json={
            "id": "ts-stdio",
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {"command": ["definitely-not-a-real-binary"], "env": {}},
            },
        },
    )
    assert r.status_code == 201, r.text
    g = await client.get("/v1/toolsets/ts-stdio")
    assert g.status_code == 200, g.text


@pytest.mark.asyncio
async def test_non_mcp_toolset_is_created_without_probing(client):
    # An internal (non-MCP) toolset has no endpoint; never probed.
    r = await client.post(
        "/v1/toolsets",
        json={"id": "ts-internal", "provider": "internal"},
    )
    assert r.status_code == 201, r.text
    g = await client.get("/v1/toolsets/ts-internal")
    assert g.status_code == 200, g.text
