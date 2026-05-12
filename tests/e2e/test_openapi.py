"""E2E: OpenAPI surface and Swagger/ReDoc gating.

Covers backlog items T0049 (OpenAPI JSON contains documented routes)
and T0050 (Swagger /docs hidden when log_level != debug).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0049_openapi_json_lists_documented_routes(
    client: httpx.AsyncClient,
) -> None:
    """T0049 — `GET /openapi.json` returns 200, parses as JSON, and
    contains the major documented route prefixes."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    paths = body.get("paths") or {}
    assert isinstance(paths, dict) and paths, body
    # Spot-check the major routers documented in 01-app-spec.md.
    expected_prefixes = (
        "/v1/health",
        "/v1/agents",
        "/v1/llm_providers",
        "/v1/toolsets",
        "/v1/workspaces",
        "/v1/workspace_providers",
        "/v1/workspace_templates",
        "/v1/collections",
        "/v1/documents",
        "/v1/internal_collections/config",
        "/v1/workers",
    )
    declared = list(paths.keys())
    for needle in expected_prefixes:
        assert any(p.startswith(needle) for p in declared), (
            f"expected at least one path starting with {needle!r} in "
            f"openapi paths; saw {declared!r}"
        )


@pytest.mark.asyncio
async def test_t0050_swagger_docs_hidden_when_not_debug(
    client: httpx.AsyncClient,
) -> None:
    """T0050 — `/docs` returns 404 under the standard bringup config,
    which sets log_level=info. Spec §1: Swagger/ReDoc are mounted only
    when `log_level=debug`.

    The bringup script renders ``log_level: info``, so the route must
    not exist. ReDoc is gated on the same condition.
    """
    docs = await client.get("/docs")
    assert docs.status_code == 404, (
        f"/docs should be 404 under non-debug config, got "
        f"{docs.status_code}: {docs.text[:200]}"
    )
    redoc = await client.get("/redoc")
    assert redoc.status_code == 404, (
        f"/redoc should be 404 under non-debug config, got "
        f"{redoc.status_code}: {redoc.text[:200]}"
    )
