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
async def test_unreachable_sse_mcp_is_rejected_and_not_persisted(client):
    # SSE is a network transport too, so an unreachable sse endpoint is probed
    # and rejected on create, exactly like http (not skipped like stdio).
    r = await client.post(
        "/v1/toolsets",
        json={
            "id": "ts-dead-sse",
            "provider": "mcp",
            "config": {
                "transport": "sse",
                "config": {"url": _DEAD_URL, "headers": {}},
            },
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["type"] == "/errors/toolset-unreachable"

    g = await client.get("/v1/toolsets/ts-dead-sse")
    assert g.status_code == 404, g.text


@pytest.mark.asyncio
async def test_allow_unreachable_false_does_not_bypass(client):
    # Only a truthy flag bypasses; allow_unreachable=false still probes and
    # rejects the dead-port create.
    r = await client.post(
        "/v1/toolsets?allow_unreachable=false",
        json=_http_mcp_body("ts-dead-false"),
    )
    assert r.status_code == 400, r.text
    assert r.json()["type"] == "/errors/toolset-unreachable"


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


# ----------------------------------------------------------------------------
# Pure classification helper: "unreachable" == genuine connection failure only.
# Non-flaky unit test of the decision that gates the reject (no real server).
# ----------------------------------------------------------------------------


def test_is_connection_failure_only_for_network_and_timeout():
    from primer.api.routers.providers import _is_connection_failure
    from primer.model.except_ import (
        AuthenticationError,
        AuthRequiredError,
        ConfigError,
        NetworkError,
        ProviderError,
        ServerError,
    )

    # Genuine connection failures -> reject the create.
    assert _is_connection_failure(NetworkError("connection refused")) is True
    assert _is_connection_failure(TimeoutError()) is True

    # The server responded (or config is the caller's) -> reachable -> allow.
    assert _is_connection_failure(AuthenticationError("401")) is False
    assert _is_connection_failure(
        AuthRequiredError("consent", auth_url="https://a/authorize", state="s")
    ) is False
    assert _is_connection_failure(ProviderError("weird 4xx")) is False
    assert _is_connection_failure(ServerError("500")) is False
    assert _is_connection_failure(ConfigError("bad config")) is False
    assert _is_connection_failure(ValueError("other")) is False


def test_informative_leaf_unwraps_group_to_network_error():
    from primer.api.routers.providers import (
        _informative_leaf,
        _is_connection_failure,
    )
    from primer.model.except_ import NetworkError

    # anyio wraps transport failures in a BaseExceptionGroup; the classified
    # NetworkError leaf must be found and judged a connection failure.
    net = NetworkError("could not connect to the MCP server")
    group = BaseExceptionGroup("unhandled", [net])
    leaf = _informative_leaf(group)
    assert leaf is net
    assert _is_connection_failure(leaf) is True


def test_informative_leaf_prefers_responded_primer_error_over_reject():
    from primer.api.routers.providers import (
        _informative_leaf,
        _is_connection_failure,
    )
    from primer.model.except_ import AuthenticationError

    # A responded-with-401 leaf inside a group is reachable -> allow.
    auth = AuthenticationError("MCP server rejected credentials (401)")
    group = BaseExceptionGroup("unhandled", [auth])
    leaf = _informative_leaf(group)
    assert leaf is auth
    assert _is_connection_failure(leaf) is False
