"""E2E: internal-collections subsystem gating.

Covers backlog item T0020 — when the subsystem hasn't been activated
(no config row), ``GET /v1/internal_collections/config`` returns 404
with the ``/errors/not-found`` envelope.

Bringup runs against a freshly-created database, so the config row is
guaranteed absent at the start of every iteration. This test does NOT
create the config (that would interfere with sibling tests that rely
on the subsystem being inactive); instead it only asserts the absence
behaviour.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0020_internal_collections_config_404_when_inactive(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/internal_collections/config")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404
    # detail should point operators at the activation path.
    assert "PUT" in body["detail"] or "configure" in body["detail"].lower()


@pytest.mark.asyncio
async def test_t0019_search_503_subsystem_inactive(
    client: httpx.AsyncClient,
) -> None:
    """T0019 — `POST /v1/agents/search` returns 503 with the manually-set
    `/errors/subsystem-inactive` slug when the subsystem is not active.

    Bringup never activates the subsystem, so this test runs against the
    inactive state without any setup of its own.
    """
    body = {"query": "anything", "top_k": 5}
    resp = await client.post("/v1/agents/search", json=body)
    assert resp.status_code == 503, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/subsystem-inactive", envelope
    assert envelope["status"] == 503


@pytest.mark.asyncio
async def test_t0060_search_top_k_zero_rejected(
    client: httpx.AsyncClient,
) -> None:
    """T0060 — Pydantic validates SearchRequest.top_k (ge=1) before the
    handler runs, so top_k=0 yields 422 even when the subsystem is
    inactive."""
    resp = await client.post(
        "/v1/agents/search", json={"query": "x", "top_k": 0},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0061_search_top_k_above_cap_rejected(
    client: httpx.AsyncClient,
) -> None:
    """T0061 — top_k=101 is above the documented cap of 100 (le=100);
    Pydantic body validation rejects it with 422 regardless of
    subsystem state."""
    resp = await client.post(
        "/v1/agents/search", json={"query": "x", "top_k": 101},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0021_bootstrap_404_when_no_config(
    client: httpx.AsyncClient,
) -> None:
    """T0021 — `POST /v1/internal_collections/bootstrap` returns 404 with
    `/errors/not-found` when no config row exists.

    The handler raises ``NotFoundError("internal collections subsystem
    is not configured; PUT /v1/internal_collections/config first.")``
    which the registry maps to status 404, slug `/errors/not-found`.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404
    assert (
        "configured" in envelope["detail"].lower()
        or "PUT" in envelope["detail"]
    )
