"""E2E: built-in (always-on) toolset listings.

Covers backlog items T0140 (`_system` toolset) and T0141 (`_workspaces`
toolset). Spec §8 lists both as always-on; the lifespan handler builds
them at startup and registers them with the ProviderRegistry, so a
`GET /v1/toolsets/{id}/tools` against either id should succeed without
any provider row existing in storage.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0140_system_toolset_lists_tools(
    client: httpx.AsyncClient,
) -> None:
    """T0140 — `GET /v1/toolsets/_system/tools` returns 200 with a
    non-empty list whose entries carry the documented fields:
    ``id``, ``description``, ``schema``, ``toolset_id``.

    NB: tools expose an ``id`` (not ``name``) — this is the canonical
    invocation handle the model uses.
    """
    resp = await client.get("/v1/toolsets/_system/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tools = body.get("tools")
    assert isinstance(tools, list) and tools, body
    for tool in tools:
        assert "id" in tool, tool
        assert "description" in tool, tool
        assert isinstance(tool["id"], str) and tool["id"], tool
        # toolset_id should consistently identify the parent toolset
        assert tool.get("toolset_id") == "_system", tool


@pytest.mark.asyncio
async def test_t0141_workspaces_toolset_lists_tools(
    client: httpx.AsyncClient,
) -> None:
    """T0141 — `GET /v1/toolsets/_workspaces/tools` returns 200 with
    the workspace-tool family. The exact tool ids are implementation
    detail, but at minimum some tool's id or description should
    reference exec / file operations.
    """
    resp = await client.get("/v1/toolsets/_workspaces/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tools = body.get("tools")
    assert isinstance(tools, list) and tools, body

    # Combine ids + descriptions into one searchable string
    haystack = " ".join(
        f"{t.get('id', '')} {t.get('description', '')}".lower()
        for t in tools
    )
    assert any(
        keyword in haystack
        for keyword in ("exec", "read", "write", "list", "file")
    ), f"no workspace-tool keyword found in tool list: {[t.get('id') for t in tools]!r}"

    # Each tool also reports the toolset_id consistently
    for tool in tools:
        assert tool.get("toolset_id") == "_workspaces", tool


# ============================================================================
# T0176 — MCP stdio toolset with unrunnable command surfaces clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0176_mcp_stdio_unrunnable_command_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0176 — Spec §8 says stdio MCP commands are constrained by
    `AppConfig.mcp_stdio_allowed_commands`. The bringup config doesn't
    set the allow-list (default null = anything goes), so we probe the
    adjacent failure path instead: an MCP stdio Toolset configured
    with a binary that doesn't exist on PATH.

    Either path yields a ConfigError-class failure inside the provider
    that the API must surface as a clean envelope (any 4xx / 502 / 503,
    NEVER 500 /errors/internal). The exact mapping is implementation
    detail; this test just pins the no-/errors/internal invariant.
    """
    toolset_id = f"mcp-bad-{unique_suffix}"
    body = {
        "id": toolset_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {
                # A binary that doesn't exist on PATH inside the
                # container. Doesn't matter what — the stdio_client
                # spawn must fail.
                "command": [
                    f"nonexistent-binary-xyz-{unique_suffix}",
                    "--serve",
                ],
            },
        },
    }
    create = await client.post("/v1/toolsets", json=body)
    assert create.status_code == 201, create.text
    try:
        # Now hit /tools — this is where the provider actually tries
        # to launch the stdio process. With the allow-list null and a
        # missing binary, this should fail at session-open and produce
        # a documented envelope, NOT /errors/internal.
        resp = await client.get(
            f"/v1/toolsets/{toolset_id}/tools",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"unexpected /errors/internal leak: {resp.text}"
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
            # Status code should be 4xx or 5xx-non-internal
            assert resp.status_code in (
                400, 401, 404, 422, 502, 503, 504,
            ), (
                f"unexpected status {resp.status_code} for unrunnable "
                f"MCP stdio command: {resp.text}"
            )
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


# ============================================================================
# T0189 — GET /v1/toolsets/{missing}/tools returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0189_tools_endpoint_on_missing_toolset_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0189 — GET /v1/toolsets/{missing}/tools on a non-existent toolset
    id returns 404 /errors/not-found. Adjacent to T0140/T0141 which
    cover the built-in success case; this is the negative envelope pin.
    """
    missing_id = f"missing-ts-{unique_suffix}"
    resp = await client.get(f"/v1/toolsets/{missing_id}/tools")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0190 — DELETE on built-in `_system` toolset returns clean 4xx envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0190_delete_builtin_system_toolset_clean_4xx(
    client: httpx.AsyncClient,
) -> None:
    """T0190 — `_system` is an always-on toolset built at lifespan
    startup (no row in storage). DELETE on its id must produce a
    documented envelope — either 404 because there's no storage row to
    delete, or a different documented 4xx if the handler special-cases
    the built-in id. The contract pin is "no /errors/internal, no 5xx".
    """
    resp = await client.delete("/v1/toolsets/_system")
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope

    # Whether DELETE returned 404 or 204, the built-in MUST still be
    # functional immediately after — its tools list still resolves.
    after = await client.get("/v1/toolsets/_system/tools")
    assert after.status_code == 200, (
        f"DELETE attempt on built-in _system toolset must not disable "
        f"the always-on provider; got {after.status_code}: {after.text}"
    )
    assert isinstance(after.json().get("tools"), list)


# ============================================================================
# T0191 — PUT on built-in `_system` toolset returns clean 4xx envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0191_put_builtin_system_toolset_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0191 — PUT on the always-on `_system` toolset id. The handler
    must not crash on the built-in identifier — must surface either a
    clean 4xx (rejection) or 200/204 (silently accepted). NEVER 500
    /errors/internal.

    Companion contract: after the PUT, the built-in /tools endpoint
    MUST still resolve — replacing the row must not disable the
    always-on provider.
    """
    body = {
        "id": "_system",
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    resp = await client.put("/v1/toolsets/_system", json=body)
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope

    # Built-in must still resolve its tools regardless
    after = await client.get("/v1/toolsets/_system/tools")
    assert after.status_code == 200, (
        f"PUT attempt on built-in _system must not disable the "
        f"always-on provider; /tools got {after.status_code}: "
        f"{after.text}"
    )
    assert isinstance(after.json().get("tools"), list)


# ============================================================================
# T0246 — DELETE built-in `_workspaces` toolset returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0246_delete_builtin_workspaces_toolset_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0246 — Mirror of T0190 (DELETE _system) for the second built-in
    toolset. The always-on _workspaces provider has no storage row;
    DELETE on its id must produce a clean envelope and the built-in
    /tools endpoint must still work afterward.
    """
    resp = await client.delete("/v1/toolsets/_workspaces")
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope

    after = await client.get("/v1/toolsets/_workspaces/tools")
    assert after.status_code == 200, (
        f"DELETE attempt on built-in _workspaces toolset must not "
        f"disable the always-on provider; /tools got "
        f"{after.status_code}: {after.text}"
    )
    assert isinstance(after.json().get("tools"), list)


# ============================================================================
# T0247 — DELETE _search toolset before subsystem activation
# ============================================================================


@pytest.mark.asyncio
async def test_t0247_delete_search_toolset_before_activation_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0247 — `_search` is the third built-in toolset, but it's only
    materialized once the internal-collections subsystem is active
    (per spec §8). Before activation, attempting to DELETE it must
    produce a clean envelope (no /errors/internal); subsequent
    activation of the subsystem must still succeed.

    The bringup never activates the subsystem at start-time, so this
    test runs on the inactive state by default.
    """
    resp = await client.delete("/v1/toolsets/_search")
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope

    # Activation must still succeed after the DELETE attempt
    embedder_id = f"emb-t0247-{unique_suffix}"
    pr = await client.post(
        "/v1/embedding_providers",
        json={
            "id": embedder_id,
            "provider": "huggingface",
            "models": [
                {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json={
                "embedding_provider_id": embedder_id,
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            },
        )
        assert put.status_code == 200, (
            f"subsystem activation should succeed after the DELETE "
            f"attempt on _search; got {put.status_code}: {put.text}"
        )
        config_created = True
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
